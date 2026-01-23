```
systemctl --user start pipeline.service
```
```
systemctl --user daemon-reload
```
```
cd ~/xAIO-URL-Agent
source venv/bin/activate
```
```
systemctl --user enable --now brave-agent.service
systemctl --user status brave-agent.service
journalctl --user -u brave-agent.service -n 200 --no-pager
```

#### Clone the repo fresh
```
cd ~
git clone https://github.com/sherafyk/xAIO-URL-Agent.git
cd xAIO-URL-Agent
```

#### See what’s modified (DO NOT guess)

From inside the repo:
```
cd ~/xAIO-URL-Agent
git status
```

You’ll likely see things like:

`config.yaml`

`.env`

maybe local script edits


possibly output dirs if something accidentally got tracked

👉 Important:
Some files (like `config.yaml` and `.env`) are supposed to be local-only and should never be committed.

```
chmod +x scripts/update_and_restart.sh  
./scripts/update_and_restart.sh  
```

```
pip install -r requirements.txt
```

```
chmod +x scripts/deploy_systemd_from_repo.sh
./scripts/deploy_systemd_from_repo.sh
```


```
chmod +x scripts/install_systemd_user.sh
./scripts/install_systemd_user.sh
```
