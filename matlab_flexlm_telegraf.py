#!/usr/bin/env python3
"""
MATLAB FlexLM License Monitoring Script for Telegraf

This script queries a FlexLM license server using lmutil and outputs metrics
in InfluxDB line protocol format for Telegraf to collect.

Required Environment Variables:
    LMUTIL         - Full path to the lmutil binary
                     Example: /usr/local/matlab/etc/glnxa64/lmutil
    LICENSE_SPEC   - License server specification in format port@hostname
                     Example: 27000@server.domain.tld

Optional Environment Variables:
    LICENSE_FILE      - Alternative license file path (if not using LICENSE_SPEC)
    MEASUREMENT       - Metric name prefix (default: flexlm_matlab)
    FLEXLM_HOST_TAG   - Override the host tag in metrics

Usage:
    python3 matlab_flexlm_telegraf.py
"""
import os
import re
import time
import socket
import subprocess

LMUTIL = os.environ.get("LMUTIL")
LICENSE_SPEC = os.environ.get("LICENSE_SPEC")
LICENSE_FILE = os.environ.get("LICENSE_FILE", "")  # optional

MEAS = os.environ.get("MEASUREMENT", "flexlm_matlab")
HOST_TAG = os.environ.get("FLEXLM_HOST_TAG", "")  # optional override for host tag

# Prepare tag values for InfluxDB line protocol.
# Escapes characters that would break tag parsing.
def escape_tag(s: str) -> str:
    return s.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")

# Build a single line-protocol record with tags, fields, and timestamp.
# Handles proper encoding for booleans, numbers, and strings.
def line(measurement: str, tags: dict, fields: dict, ts_ns: int) -> str:
    tag_str = ",".join([f"{k}={escape_tag(str(v))}" for k, v in tags.items() if v != "" and v is not None])
    field_parts = []
    for k, v in fields.items():
        if isinstance(v, bool):
            field_parts.append(f"{k}={str(v).lower()}")
        elif isinstance(v, int):
            field_parts.append(f"{k}={v}i")
        elif isinstance(v, float):
            field_parts.append(f"{k}={v}")
        else:
            # string field
            sv = str(v).replace("\\", "\\\\").replace('"', '\\"')
            field_parts.append(f'{k}="{sv}"')
    field_str = ",".join(field_parts)
    if tag_str:
        return f"{measurement},{tag_str} {field_str} {ts_ns}"
    return f"{measurement} {field_str} {ts_ns}"

# Invoke lmutil lmstat with the configured target.
# Returns stdout or embeds errors in a prefixed string for reporting.
def run_lmstat() -> str:
    cmd = [LMUTIL, "lmstat", "-a"]
    # Some setups require -c <licfile> or -c <port@host>
    if LICENSE_FILE:
        cmd += ["-c", LICENSE_FILE]
    elif LICENSE_SPEC:
        cmd += ["-c", LICENSE_SPEC]

    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=20)
    except Exception as e:
        return f"__ERROR__ {e}"
    if p.returncode != 0:
        # still return stderr so telegraf can ingest error metric
        return "__ERROR__ " + (p.stderr.strip() or f"lmutil exit {p.returncode}")
    return p.stdout

# Parse lmstat output into server status plus per-feature usage.
# Captures issued/in-use counts and active usernames per feature.
def parse(text: str):
    # Status
    license_server_up = None
    vendor_up = None
    server_host = ""
    server_port = ""

    # Feature usage
    # "Users of MATLAB:  (Total of 25 licenses issued;  Total of 5 licenses in use)"
    # Note: "license" can be singular when count is 1
    feature_re = re.compile(
        r'^Users of\s+(.+?):\s+\(Total of\s+(\d+)\s+licen[sc]es? issued;\s+Total of\s+(\d+)\s+licen[sc]es? in use\)',
        re.IGNORECASE
    )

    # Checkout lines typically like:
    #   alex.user compute1.example.net /dev/pts/16 (v45) (licsrv.example.net/27000 1959), start Fri 1/9 19:35, PID: 562721
    # We capture username as first token on a line with 4+ spaces of indentation (checkout lines are more indented than metadata)
    checkout_user_re = re.compile(r'^\s{4,}([^\s"]+)\s+')

    current_feature = None
    in_checkout_section = False  # Track if we're in the checkout section
    features = {}  # feature -> dict(issued, in_use, users_set)
    for line_in in text.splitlines():
        s = line_in.rstrip("\n")

        # License server status line: "License server status: 27000@licsrv.example.net"
        if s.startswith("License server status:"):
            # Best-effort parse
            m = re.search(r'License server status:\s+(\d+)@([^\s]+)', s)
            if m:
                server_port, server_host = m.group(1), m.group(2)

        # Server UP line: "licsrv.example.net: license server UP (MASTER) v11.19.6"
        if "license server" in s and (" UP " in s or " DOWN " in s):
            if "license server UP" in s:
                license_server_up = True
            elif "license server DOWN" in s:
                license_server_up = False

        # Vendor daemon status: "MLM: UP v11.19.6"
        if re.search(r'^\s*MLM:\s+UP\b', s):
            vendor_up = True
        if re.search(r'^\s*MLM:\s+DOWN\b', s):
            vendor_up = False

        # Feature header
        m = feature_re.match(s)
        if m:
            current_feature = m.group(1).strip()
            issued = int(m.group(2))
            in_use = int(m.group(3))
            features[current_feature] = {"issued": issued, "in_use": in_use, "users": set()}
            in_checkout_section = False  # Reset checkout section flag, will be set when we see feature details
            continue

        # Blank line - skip but don't change state yet
        if not s.strip():
            continue

        # Feature detail line starting with quotes - we're entering potential checkout section
        if current_feature and s.lstrip().startswith('"'):
            in_checkout_section = True
            continue

        # Checkout line under a feature block (only process if in checkout section)
        if current_feature and in_checkout_section and s.startswith(" "):
            mu = checkout_user_re.match(s)
            if mu and current_feature in features:
                user = mu.group(1)
                features[current_feature]["users"].add(user)

    return {
        "server_host": server_host,
        "server_port": server_port,
        "license_server_up": license_server_up,
        "vendor_up": vendor_up,
        "features": features,
    }

# Entry point for Telegraf exec input.
# Validates config, runs lmstat, and emits metrics in line protocol.
def main():
    ts_ns = int(time.time() * 1e9)

    tags_base = {}
    if HOST_TAG:
        tags_base["host"] = HOST_TAG
    else:
        tags_base["host"] = socket.gethostname() or "unknown"

    config_errors = []
    if not LMUTIL:
        config_errors.append("LMUTIL environment variable not set")
    if not (LICENSE_SPEC or LICENSE_FILE):
        config_errors.append("LICENSE_SPEC or LICENSE_FILE must be provided")

    if config_errors:
        tags = dict(tags_base)
        tags["license_server"] = LICENSE_SPEC or LICENSE_FILE or "unknown"
        print(line(MEAS, tags, {"ok": False, "error": "; ".join(config_errors)}, ts_ns))
        return

    raw = run_lmstat()

    # If we got an error from runner, emit a single error metric
    if raw.startswith("__ERROR__"):
        err = raw.replace("__ERROR__", "", 1).strip()
        tags = dict(tags_base)
        tags["license_server"] = LICENSE_SPEC or "unknown"
        print(line(MEAS, tags, {"ok": False, "error": err}, ts_ns))
        return

    data = parse(raw)
    if not HOST_TAG and data.get("server_host"):
        tags_base["host"] = data["server_host"]
    license_server = f"{data['server_port']}@{data['server_host']}" if data["server_host"] else (LICENSE_SPEC or "unknown")

    # Overall status metric
    tags = dict(tags_base)
    tags["license_server"] = license_server
    fields = {}
    if data["license_server_up"] is not None:
        fields["license_server_up"] = data["license_server_up"]
    if data["vendor_up"] is not None:
        fields["vendor_mlm_up"] = data["vendor_up"]

    if data["license_server_up"] is True and data["vendor_up"] is True:
        fields["ok"] = True
        fields["status"] = "up"
    elif data["license_server_up"] is False or data["vendor_up"] is False:
        fields["ok"] = False
        fields["status"] = "down"
    else:
        fields["status"] = "unknown"

    print(line(MEAS, tags, fields, ts_ns))

    # Feature metrics
    for feat, v in data["features"].items():
        ftags = dict(tags_base)
        ftags["license_server"] = license_server
        ftags["feature"] = feat

        issued = int(v["issued"])
        in_use = int(v["in_use"])
        free = max(issued - in_use, 0)
        overutilized = in_use > issued

        ffields = {
            "issued": issued,
            "in_use": in_use,
            "free": free,
            "users": int(len(v["users"])),
        }
        if overutilized:
            ffields["overutilized"] = True
        print(line(MEAS + "_feature", ftags, ffields, ts_ns))

        # Per-user metrics
        for user in v["users"]:
            utags = dict(tags_base)
            utags["license_server"] = license_server
            utags["feature"] = feat
            utags["username"] = user
            ufields = {"active": 1}
            print(line(MEAS + "_user", utags, ufields, ts_ns))


if __name__ == "__main__":
    main()

