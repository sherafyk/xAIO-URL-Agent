## Notes

* Use a dedicated user-data-dir for the agent so it doesn't interfere with your daily browsing profile.
* If multiple Brave instances are running, the debugging port can become unreliable. Prefer a dedicated instance for the agent.

````

## `docs/systemd.md`

```md
# systemd (user services)

We run the workers as user-level systemd timers so they survive reboots and run without a terminal.

## Where units live
User units:
~/.config/systemd/user/

Repo copies (source-controlled):
systemd/user/

## Common commands

Reload definitions:
```bash
systemctl --user daemon-reload
````

Enable a timer:

```bash
systemctl --user enable --now url-agent.timer
```

Disable a timer:

```bash
systemctl --user disable --now url-agent.timer
```

See timers:

```bash
systemctl --user list-timers
```

Logs:

```bash
journalctl --user -u url-agent.service -n 200 --no-pager
journalctl --user -u condense-agent.service -n 200 --no-pager
```

Follow logs live:

```bash
journalctl --user -u url-agent.service -f
```

## Overlap prevention

Use `flock` in ExecStart so multiple invocations don't overlap.

````

## `docs/troubleshooting.md`

```md
# Troubleshooting

## It keeps opening Brave windows / too many tabs
- Ensure you are attaching to an existing Brave instance (CDP), not launching a new instance per URL.
- Confirm only one Brave debug instance is running.

## CDP not reachable
```bash
curl -s http://127.0.0.1:9222/json/version
````

If that fails:

* Brave isn't running with `--remote-debugging-port=9222`
* another process is using the port

## Sheet updates not working

Check:

* `secrets/service_account.json` exists
* the service account email has access to the sheet
* spreadsheet URL and worksheet name are correct in config.yaml

## systemd timer runs but nothing happens

* check logs with journalctl
* run the script manually to see exceptions
* verify working directory paths in the unit file

## AI errors

* confirm OPENAI_API_KEY is set
* confirm scf-export-content.json exists and is referenced correctly
* inspect `*.response_raw.json` outputs for schema issues

````

---

---

## After this, new machine setup becomes:

```bash
git clone <repo>
cd xAIO-url-agent
chmod +x scripts/*.sh
./scripts/bootstrap_ubuntu.sh

# put secrets/service_account.json
cp /path/to/service_account.json ./secrets/service_account.json

# set OpenAI key
cp .env.example .env
nano .env
source .env

./scripts/doctor.sh
./scripts/install_systemd_user.sh
```

---

If you want, paste your current repo root `ls -la` (just filenames) and I’ll tell you what to move into `src/` vs leave at root so the repo reads cleanly to both humans and an agent.

