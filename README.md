Bluetooth-Scanner-v2

Requirements
    - bleak 0.22

AI : Claude Opus 5.5 Medium

Thought for : 9 min 34 sec

Prompt v1 : boutta spit some knowledge at you. I need it to have a GUI, scan for all available devices in the area. Refresh every 2 seconds to see all devices more, sort it from weakest signal to strongest. ALlow the user to connect to a selected device and see that devices's services. then allow the user to select a service and see the characteristics of that service. Rounding it out, I want the user to read/write/notify characterisitcs, then allow the user to disconnect and also allow the user to cancel a connectio attempt. Finally, have the user's selction keep selected even as a device would move around the list. U got this, positive reinforcement. Take your time and write it so it works first try

AI Response : I’ve built the GUI version as ble_explorer_gui.py. Copy it and requirements.txt into your repo, then run pip install -r requirements.txt and python ble_explorer_gui.py. It covers everything on your list, but it hasn’t been run on real hardware yet.

How I tested it: this workspace has no Bluetooth or display, so I couldn’t open the window here. Instead:

Every window and widget call was checked against tkinter’s official type definitions with two checkers, and both came back clean.
I ran the full app against a fake Bluetooth stack and a fake tkinter, on Python 3.11, 3.12 and 3.13. All 68 checks passed on each.
The fake devices covered changing signal strengths, a slow device for cancelling, one that refuses to connect, and one that drops off by itself.

That proves the app’s logic. It doesn’t prove your adapter or the real window behave the same, so give it one real run.

Your list:

Live scan: the device list refreshes every 2 seconds, sorted weakest to strongest. Devices not heard from for 30 seconds drop off.
Selection stays put: your selected device stays selected as it moves around the list. The test moved it to several different positions and it held every time.
Connect, Cancel connection, Disconnect: double-clicking a device also connects. Cancel stops the attempt and cleans up the half-made connection. The connected device is highlighted in green.
Services and characteristics: pick a service to see its characteristics, each with its properties and handle.
Read / Write / Notify:
Each button only turns on when the selected characteristic supports it.
Write accepts text, hex (01 A0 FF) or decimal bytes, and bad input gets a clear error message.
Values are shown as hex, plus text and number versions when those make sense.
Notifications appear live in the log.

Things to know:

Scan pauses while connecting. Connecting while scanning is unreliable on Linux, so the scan stops during a connection attempt and starts again right after.
Unexpected disconnects are handled. If a device drops, the app clears the services and logs it instead of freezing or crashing.
Setup per system:
Linux: you may need sudo apt install python3-tk, and sudo or the bluetooth group.
macOS: allow Bluetooth for your terminal in Privacy settings.
Windows: no extra setup.