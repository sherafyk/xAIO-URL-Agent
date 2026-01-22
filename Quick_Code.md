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
