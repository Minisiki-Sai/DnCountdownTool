Npcap installer bundle slot
===========================

Npcap Free/Demo installers must not be redistributed with this application.
Only a Npcap OEM installer covered by a redistribution license may be bundled.

For the pure PyInstaller build, set both variables before running the build:

  DN_SIMULATOR_NPCAP_OEM_INSTALLER=C:\path\to\licensed-npcap-oem.exe
  DN_SIMULATOR_NPCAP_REDISTRIBUTION_LICENSED=1

The build stages it as npcap-installer.exe, embeds it into both releases, and
removes the staged source copy when the build finishes. Without an OEM
installer, the application opens the official Npcap download page or lets the
user select an installer already downloaded on that computer.
