# GRL V_QI Utility - Build Instructions

## 📋 Prerequisites

Before building, ensure you have the following installed:

1. **Python 3.8+** - Check with: `python --version`
2. **PyInstaller** - Install with: `pip install pyinstaller`
3. **Dependencies** - Install with: `pip install -r requirements.txt`

## 🚀 Quick Build

### Method 1: Using Build Script (Easiest)

**Windows Command Prompt / PowerShell:**
```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
.\build.bat
```

**Or using PowerShell script:**
```powershell
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
.\build.ps1
```

### Method 2: Direct PyInstaller Command

```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
pyinstaller packaging/GRL_V_QI_UTILITY.spec --clean --noconfirm
```

## 📦 Build Output

After a successful build:
- **Executable**: `dist/GRL_V_QI_UTILITY.exe`
- **Build files**: `build/` directory (can be deleted after build)

## 🎨 What's Included

The executable includes:
- ✅ Updated GRL logo (`grl_updated_logo.jpg`)
- ✅ Dark theme with orange accents
- ✅ All required Python packages
- ✅ Matplotlib plotting libraries
- ✅ Configuration files

## 🛠️ Build Configuration

The build is configured via `packaging/GRL_V_QI_UTILITY.spec`:
- **Type**: Single-file executable (--onefile)
- **Console**: Windowed mode (no console window)
- **Icon**: GRL logo icon
- **Compression**: UPX enabled

## ⚠️ Troubleshooting

### Issue: "pyinstaller: command not found"
**Solution**: Install PyInstaller
```bash
pip install pyinstaller
```

### Issue: Missing dependencies
**Solution**: Install all requirements
```bash
pip install -r requirements.txt
```

### Issue: Build succeeds but exe won't run
**Solution**:
1. Check antivirus hasn't quarantined the exe
2. Run from command prompt to see error messages
3. Rebuild with `--clean` flag

### Issue: Large file size
**Solution**: The executable is large (~100-200MB) because it includes:
- Complete Python runtime
- Matplotlib and all plotting libraries
- All dependencies

This is normal for PyInstaller builds.

## 📝 Clean Build

To perform a clean build (removes all previous build artifacts):

```bash
cd d:\V_QI\PoC\python_scripts\cps_ui_tool
rmdir /s /q dist build
pyinstaller packaging/GRL_V_QI_UTILITY.spec --clean --noconfirm
```

## 🎯 Distribution

The `dist/GRL_V_QI_UTILITY.exe` is a standalone executable that can be:
- Copied to any Windows machine
- Run without Python installation
- Distributed to users

**Note**: First run might take a few seconds as the executable unpacks.

## 📧 Support

For build issues, check:
1. Python version compatibility
2. All dependencies installed
3. Antivirus not blocking PyInstaller
4. Sufficient disk space (~500MB for build process)
