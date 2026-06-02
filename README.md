Docker Miner Live Dashboard Monitor

This is a dynamic, real-time command line dashboard and performance tracker for mining Pearl inside Docker containers. This Python script automatically aggregates information from your active container logs, localized historic files, your graphics card telemetry, online market pricing index APIs, and pool data to build a consolidated performance monitor.

Core Features

Log Ingestion Pipeline: Automatically scans historical Docker logs on startup, filters past submissions, and maps continuous metrics without slowing down your computer memory.

Real Time Stats: Monitors active Hardware Speed, Pool Efficiency, share submission pacing, work attempts, and stale share metrics.

Hardware Telemetry Integration: Uses Nvidia Management Library to pull exact live wattage draws and core temperatures directly from your graphics card.

AlphaPool Stats Integration: Automatically fetches global statistics including network speed, pool speed, online participation counts, and recent blocks discovered.

Wallet Monitoring: Optional view to show your pending unpaid mining balance, lifetime payouts, and historical transaction dates.

Live Profit Matrix Forecasting: Automatically fetches spot pricing and historical data for Bitcoin and Pearl. It calculates dynamic Gross Revenue, Power Costs, and Net Profits.

Advanced Time of Use Billing Engine: Supports multi tier chronological electricity cost calculations such as Summer Peak versus Off Peak versus Winter Baseline configurations.

Prerequisites and Setup

This script works on Windows, Linux, or macOS where your mining container is running. Follow these simple steps to set it up.

Step One: Install Python

Ensure Python version 3.8 or newer is installed on your computer. If you are on Windows, download it from python.org and make sure you check the box that says Add Python to PATH during the installation process. If you are on Linux, run the command sudo apt update followed by sudo apt install python3 python3-pip in your terminal.

Step Two: Install Hardware Tracking Dependencies

Open your Command Prompt or Terminal and run the following command to allow Python to communicate with Nvidia hardware:

`pip install nvidia-ml-py`

Step Three: Configure Your Settings

Open live_monitor.py in any plain text editor like Notepad. Locate the USER CONFIGURATIONS block at the top and customize your settings. You can change the container name to match your active Docker setup, paste your wallet address, or adjust your electricity cost settings.

<img width="550" height="250" alt="image" src="https://github.com/user-attachments/assets/6f1dc99d-cfac-4369-abf8-74f2b78f4edc" />

How to Run It

First, ensure your mining Docker container is currently up and running.

Second, open your terminal or command prompt in the exact folder containing your live_monitor.py file.

Third, launch the dashboard by typing this command:

`python live_monitor.py`

During Phase One, the script will digest historical records to calibrate your averages. During Phase Two, the terminal window will lock into a continuous dashboard view, automatically painting live logs, accepted shares, global statistics, and profits every few seconds.

To stop the script at any time, press the CTRL and C keys on your keyboard together to exit safely.

Troubleshooting Common Issues

If you see a Docker Error message, your container named in the script configuration is either spelled wrong or the Docker Desktop application is not running.

If your Hardware Speed or Pool Stats show FETCHING, the script simply requires a few moments to wait for the miner inside your container to print out its first operational status report lines.

If your Power or Temperature reads zero, this means you are either running an AMD graphics card or your Nvidia drivers are currently inaccessible. The program will skip hardware telemetry gracefully and continue tracking everything else.



