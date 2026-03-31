# GRL V_QI Utility

A Windows desktop application for wireless power transfer testing and analysis, featuring real-time register monitoring, FSK packet capture, plotting capabilities, and ESP32 firmware updates.

![Version](https://img.shields.io/badge/version-1.0-blue)
![Platform](https://img.shields.io/badge/platform-Windows-blue)
![Python](https://img.shields.io/badge/python-3.8+-blue)

---

## 📋 Features

### Core Functionality
- **Network Client**: TCP socket connection to test devices (default port 5002)
- **Register Management**: Read/write registers with comprehensive register database
- **Console Interface**: Real-time command logging and device communication
- **Plot Panel**: Matplotlib-based data visualization with multiple plot types
- **FSK Receiver**: UDP-based FSK packet capture and analysis
- **Firmware Updates**: ESP32 firmware flashing via esptool

### UI Components
- Clean, professional interface with GRL branding
- Light theme with navy blue accents (`#003087`)
- Tabbed interface for organized workflows:
  - Console: Command history and device logs
  - Plot: Real-time data visualization
  - FSK: Packet capture and analysis
  - Registers: Register editor with autocomplete
  - FW Update: ESP32 firmware flashing tool

---

## 🚀 Quick Start

### Running from Source

1. **Install Python 3.8+**
2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Run the application:**
   ```bash
   python src/app.py
   ```

### Building Executable

See **[BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md)** for detailed build instructions.

**Quick build:**
```bash
.\build.bat
```

Output: `dist\GRL_V_QI_UTILITY.exe`

---

## 📁 Project Structure

```
cps_ui_tool/
├── src/                      # Source code
│   ├── app.py               # Main application entry point
│   ├── net_client.py        # TCP network client
│   ├── ui_plot.py           # Plotting panel
│   ├── udp_fsk.py           # FSK receiver
│   ├── esp_flasher.py       # ESP32 firmware flasher
│   ├── protocol.py          # Protocol utilities
│   ├── logger.py            # Logging utilities
│   ├── project_paths.py     # Path management
│   └── analyze_pcap.py      # Packet analysis tools
│
├── assets/                   # Application assets
│   ├── grl_logo.png         # Application logo
│   └── grl_logo.ico         # Window icon
│
├── include/                  # Configuration files
│   └── regset.txt           # Register definitions
│
├── packaging/               # Build configuration
│   └── GRL_V_QI_UTILITY.spec # PyInstaller spec file
│
├── build.bat                # Windows build script
├── build.ps1                # PowerShell build script
├── requirements.txt         # Python dependencies
└── README.md               # This file
```

---

## 🛠️ Technology Stack

- **Python 3.8+** - Core language
- **tkinter** - GUI framework
- **matplotlib** - Data visualization
- **pyserial** - Serial communication
- **esptool** - ESP32 firmware flashing
- **PyInstaller** - Executable packaging

---

## 📖 Documentation

- **[BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md)** - Detailed build guide
- **[FINAL_CHANGES_SUMMARY.md](FINAL_CHANGES_SUMMARY.md)** - Current project state
- **[LOGO_UPDATE_SUMMARY.md](LOGO_UPDATE_SUMMARY.md)** - Logo asset information

---

## 🎨 Branding

### Current Theme
- **Primary Color**: Navy Blue `#003087`
- **Accent Color**: Light Blue `#1A50A5`
- **Background**: Light Blue-White `#F0F4FB`
- **Text**: Dark Navy `#002060`

### Assets
- Logo: `assets/grl_logo.png`
- Icon: `assets/grl_logo.ico`

---

## 🔧 Configuration

### Network Settings
- Default port: `5002`
- Configurable timeout and retry settings
- Automatic reconnection support

### Register Database
Register definitions are loaded from `include/regset.txt` with format:
```
RegisterName, Address, Size
```

---

## 📝 Usage

### Connecting to Device
1. Enter IP address and port (default: 5002)
2. Click **Connect**
3. Device status shown in top bar

### Register Operations
1. Select register from dropdown (autocomplete enabled)
2. Enter value in hex format (e.g., `0x1234`)
3. Click **Write** to send, or **Read** to retrieve current value

### FSK Capture
1. Navigate to **FSK** tab
2. Click **Start Capture** to begin UDP listening
3. Packets displayed in real-time
4. Use **Save** to export capture data

### Firmware Update
1. Navigate to **FW Update** tab
2. Select chip type (ESP32/ESP32-S2/ESP32-C3)
3. Choose firmware binary file
4. Select COM port
5. Click **Flash** to begin update

---

## 🐛 Troubleshooting

### Application won't start
- Ensure Python 3.8+ is installed
- Verify all dependencies: `pip install -r requirements.txt`
- Check for port conflicts (default 5002)

### Build fails
- Install PyInstaller: `pip install pyinstaller`
- Ensure all assets exist in `assets/` directory
- Try clean build: `.\build.bat` (cleans automatically)

### Connection issues
- Verify device IP address and port
- Check firewall settings
- Ensure device is powered and network-accessible

---

## 📦 Building for Distribution

The application can be packaged as a standalone executable:

1. **Single-file executable** - All dependencies bundled
2. **No Python required** - Runs on any Windows machine
3. **~34 MB size** - Includes Python runtime and all libraries

See [BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md) for details.

---

## 📄 License

Copyright © 2026 GRL. All rights reserved.

---

## 🔗 Additional Resources

- Python: https://www.python.org/
- tkinter: https://docs.python.org/3/library/tkinter.html
- matplotlib: https://matplotlib.org/
- PyInstaller: https://pyinstaller.org/

---

**Last Updated**: March 31, 2026
**Status**: Production Ready
