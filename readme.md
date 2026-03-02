<img width="1080" height="720" alt="Screenshot From 2026-02-10 22-47-16" src="https://github.com/user-attachments/assets/7ef8e41a-3c6a-41b3-8223-c75da364d33e" />

# AZNet BBS

AZNet BBS is a Python-based Bulletin Board System project inspired by classic BBS software. The project is under active development.

---

## Features

- Threaded messages (currently implemented)
- Configurable via `config.py`

### Future Goals

- FTN (FidoNet) support
- User voting system
- Doors / PFiles integration
- Text and GFiles support
- One-line user messages
- CNet-style "JoinLink"

---

## Requirements

- Python 3.13.7 (current development version)
- Additional dependencies to be added (placeholder for `requirements.txt`)

---

## Installation

Clone the repository:

```bash
git clone https://github.com/mobbyg/aznet_bbs.git
cd aznet_bbs
```
## Usage

Start the BBS Server
```python run_server.py```

Start the Admin Panel
```python run_panel.py```


No environment variables are required at this stage.

## Configuration

Edit ```config.py``` to adjust server settings, paths, or other options. Configuration options are expected to evolve in upcoming iterations.

## License

This project is licensed under the GPL License.

## Contribution

Contribution guidelines: TBA

---
## Milestone 1 (COMPLETED)<br>
✅ Async telnet server<br>
✅ User login system<br>
✅ SQLite database<br>
✅ ANSI rendering<br>
✅ Basic main menu<br>
✅ CNet-style text handling<br>


## Milestone 2 (🟡 IN PROGRESS)

⚠️ SysOp Control Panel (Created and talking to API/Server, no controls wired in yet)<br>
❌ IPC<br>
❌ real-time monitoring (This would be a separate GUI/TUI application)<br>

## Milestone 3 🟡 IN PROGRESS (~60% done)<br>

✅ Message bases: reading, posting, thread navigation, visual editor<br>
⚠️ Message bases: still need RESPOND to posts (replies), thread depth display<br>
❌ File areas: upload/download system (not started)<br>
⚠️ Access groups: basic system exists, but no full management (need VDE screens for group creation/editing)<br>
❌ SysOp configuration UI: not started (would be like CNet's CONFIG program)<br>

## Milestone 4 (Planned)

   * SSH support
   * Doors / external programs
   * Networking concepts

