# 🎙️ LiveKit Voice AI Agent - Production Ready

A complete LiveKit voice AI agent setup with SIP integration, management dashboard, and Docker infrastructure. Ready to deploy on Linux servers and develop on Windows.

---

## 📋 What's Included

- **Voice AI Agent** - Real-time conversational AI with voice capabilities
- **SIP Integration** - Connect with phone systems (FreeSWITCH/Asterisk)
- **Management Dashboard** - Web-based control panel
- **Docker Infrastructure** - LiveKit server and Redis setup
- **Multi-Platform** - Works on Linux (production) and Windows (development)

---

## 🚀 Quick Start

### **Linux Setup (Production Server)**

**1. Clone the repository**
```bash
git clone https://github.com/shahzaibaqeel/LiveKit_Local_Deployement.git
cd LiveKit_Local_Deployement
```

**2. Setup Python environment**
```bash
cd agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements-linux.txt
```

**3. Download AI models** (if needed)
```bash
# Add your model download commands here
# Example: wget https://your-model-url.com/model.zip
```

**4. Start LiveKit infrastructure**
```bash
cd ../infrastructure
docker-compose up -d
```

**5. Run the agent**
```bash
cd ../agent
source venv/bin/activate
python src/main.py  # or your main entry file
```

**6. Start the dashboard** (optional)
```bash
cd ../dashboard
# Follow dashboard-specific setup instructions
```

---

### **Windows Setup (Development)**

**1. Clone the repository**
```bash
git clone https://github.com/shahzaibaqeel/LiveKit_Local_Deployement.git
cd LiveKit_Local_Deployement
```

**2. Setup Python environment**
```bash
cd agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements-window.txt
```

**3. Open in VS Code**
```bash
code .
```

**Note:** You don't need AI models or Docker on Windows for code development. Models only run on the Linux production server.

---

## 📁 Project Structure
```
LiveKit_Local_Deployement/
│
├── agent/                          # AI Voice Agent
│   ├── src/                        # Python source code
│   ├── requirements-linux.txt      # Linux dependencies
│   ├── requirements-window.txt     # Windows dependencies
│   ├── Dockerfile                  # Container setup
│   └── README.md                   # Agent documentation
│
├── dashboard/                      # Management Dashboard
│   ├── app/                        # Dashboard application
│   └── docker-compose.yml          # Dashboard containers
│
├── infrastructure/                 # LiveKit Infrastructure
│   ├── docker-compose.yaml         # LiveKit + Redis setup
│   └── livekit-config.yaml         # LiveKit configuration
│
├── dispatch-rule.json              # SIP dispatch rules
└── update-trunk.json               # SIP trunk configuration
```

---

## 🔄 Development Workflow

### **Making Changes on Windows**

1. **Edit code** in VS Code
2. **Check changes:**
```bash
   git status
```
3. **Stage changes:**
```bash
   git add .
```
4. **Commit with message:**
```bash
   git commit -m "Fixed audio processing bug"
```
5. **Push to GitHub:**
```bash
   git push
```

### **Update Linux Server**

1. **Pull latest changes:**
```bash
   cd ~/livekit-project
   git pull
```
2. **Restart services if needed:**
```bash
   docker-compose restart
   # or restart agent manually
```

### **Making Changes on Linux**

1. **Make changes** and test
2. **Commit and push:**
```bash
   git add .
   git commit -m "Updated configuration"
   git push
```
3. **Update Windows:**
```bash
   git pull
```

---

## 🛠️ Common Commands

### **Git Commands**
```bash
git status              # See what changed
git pull                # Get latest updates
git add .               # Stage all changes
git commit -m "msg"     # Commit with message
git push                # Upload to GitHub
git log                 # View history
```

### **Linux Agent Commands**
```bash
source venv/bin/activate    # Activate virtual environment
deactivate                  # Deactivate virtual environment
python src/main.py          # Run agent
```

### **Docker Commands**
```bash
docker-compose up -d        # Start services
docker-compose down         # Stop services
docker-compose logs -f      # View logs
docker-compose restart      # Restart services
docker ps                   # List running containers
```

---

## ⚙️ Configuration

### **LiveKit Server**
Edit `infrastructure/livekit-config.yaml` for:
- API keys
- Port settings
- TURN server config
- Webhook URLs

### **SIP Integration**
- **Dispatch rules:** `dispatch-rule.json`
- **Trunk settings:** `update-trunk.json`

### **Agent Settings**
Check `agent/src/config.py` or environment variables for:
- AI model selection
- Voice settings
- API endpoints

---

## 📦 Requirements

### **Linux Server**
- Ubuntu 20.04+ or similar
- Docker & Docker Compose
- Python 3.9+
- 4GB+ RAM (8GB recommended)
- Internet connection for model downloads

### **Windows Development**
- Windows 10/11
- Python 3.9+
- Git
- VS Code (recommended)

---

## 🔧 Troubleshooting

### **"Module not found" error**
```bash
# Make sure virtual environment is activated
source venv/bin/activate  # Linux
venv\Scripts\activate     # Windows

# Reinstall requirements
pip install -r requirements-linux.txt    # Linux
pip install -r requirements-window.txt   # Windows
```

### **Docker services won't start**
```bash
# Check logs
docker-compose logs

# Restart services
docker-compose down
docker-compose up -d
```

### **Git push failed**
```bash
# Pull first, then push
git pull
git push
```

### **Permission denied on Linux**
```bash
# Make sure you're in the right directory
cd ~/livekit-project

# Check file permissions
ls -la
```

---

## 📝 Notes

- **Models are NOT in this repo** - They're too large for GitHub. Download separately on your Linux server.
- **Virtual environment (`venv/`) is NOT tracked** - Create it fresh on each machine.
- **Environment files (.env) are NOT tracked** - Create them manually with your API keys.
- **Windows is for development only** - Production deployment requires Linux.

---

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature-name`
3. Commit changes: `git commit -m "Add feature"`
4. Push to branch: `git push origin feature-name`
5. Open a Pull Request

---

## 📄 License

See `agent/LICENSE` for details.

---

## 🆘 Need Help?

- Check the `agent/README.md` for agent-specific documentation
- Review `dashboard/README.md` for dashboard setup
- Check Docker logs: `docker-compose logs -f`
- Verify configuration files in `infrastructure/`

---

**Built with LiveKit • Ready for Production • Easy to Deploy**
