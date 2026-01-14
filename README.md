# MATLAB FlexLM Telegraf Monitor

This directory contains a Telegraf **exec** input script plus its companion configuration that collect FlexLM license usage statistics from a MATLAB license server and forward them to InfluxDB.

## Contents

| File | Purpose |
| --- | --- |
| `matlab_flexlm_telegraf.py` | Python helper that shells out to `lmutil lmstat`, parses license status, and emits InfluxDB line protocol metrics (overall server state, feature utilization, and active users). |
| `matlab_flexlm.conf` | Telegraf input definition that runs the Python helper every 60 seconds with the required environment variables. |

## Requirements

- Python 3.8+ (system `python3`)
- `lmutil` binary that matches your FlexLM deployment (path exported via the `LMUTIL` environment variable)
- Telegraf agent with the exec input plugin enabled
- Sudo/root access for installing files into `/usr/local/bin` and `/etc/telegraf/telegraf.d`

## Deploying

1. Review and adjust `matlab_flexlm.conf` so the `LMUTIL`, `LICENSE_SPEC`, and other environment variables match the desired license server.
2. Copy the helper and config into their runtime locations (requires sudo/root):

  ```bash
  cd /root/matlab_monitor
  sudo install -m 0755 matlab_flexlm_telegraf.py /usr/local/bin/matlab_flexlm_telegraf.py
  sudo install -m 0644 matlab_flexlm.conf /etc/telegraf/telegraf.d/matlab_flexlm.conf
  ```

3. Restart or reload Telegraf so it picks up the updated exec plugin configuration, for example:

   ```bash
   sudo systemctl restart telegraf
   ```

These commands install the helper to `/usr/local/bin/matlab_flexlm_telegraf.py` with execute permissions and place the Telegraf input file at `/etc/telegraf/telegraf.d/matlab_flexlm.conf`.

## Configuration Notes

The Python helper honors the following environment variables (surfaced via the Telegraf input):

- `LMUTIL` (required): absolute path to the `lmutil` binary.
- `LICENSE_SPEC` **or** `LICENSE_FILE` (required): either `port@hostname` or a path to a FlexLM license file.
- `MEASUREMENT` (optional): custom measurement prefix (defaults to `flexlm_matlab`).
- `FLEXLM_HOST_TAG` (optional): override the `host` tag emitted into line protocol.

Adjust the `environment` array in `matlab_flexlm.conf` to set these values. You can also tag the metrics via `[inputs.exec.tags]`.