---
name: VPS Deployment
description: Deploy projects to the VPS server. Contains VPS connection details, deployment commands, and server configuration.
---

# VPS Server Information

## Connection Details
- **Host**: 192.168.68.218
- **Username**: webadmin
- **Password**: admin
- **SSH Command**: `sshpass -p 'admin' ssh -o StrictHostKeyChecking=no webadmin@192.168.68.218`
- **SCP Command**: `sshpass -p 'admin' scp -o StrictHostKeyChecking=no <local_file> webadmin@192.168.68.218:<remote_path>`

## Server Specs
- **Hostname**: labeltech
- **OS**: Ubuntu (Kernel 6.8.0-136-generic)
- **Architecture**: x86_64
- **RAM**: 3.8 GB
- **Disk**: 15 GB (on /dev/mapper/ubuntu--vg-ubuntu--lv)
- **Swap**: 3.1 GB

## Installed Software
- Python 3.12.3
- Docker 29.0.4
- Git 2.43.0
- systemctl available

## Deployed Projects

### Kids Coloring Book Generator (coloring_web)
- **Repo**: https://github.com/MahmedRahman/coloring_web.git
- **Path on VPS**: /home/webadmin/coloring_web
- **Container Name**: coloring_web
- **Port**: 5001 (mapped from container port 5000)
- **URL**: http://192.168.68.218:5001
- **Deployment Method**: Docker Compose
- **Deploy Command**:
  ```bash
  sshpass -p 'admin' ssh -o StrictHostKeyChecking=no webadmin@192.168.68.218 \
    "cd /home/webadmin/coloring_web && git pull && docker compose up -d --build"
  ```

## Quick Deployment Steps
1. Push changes to GitHub
2. SSH into VPS
3. `git pull` to get latest changes
4. `docker compose up -d --build` to rebuild and restart

## Notes
- Nginx is NOT installed on this server
- The `/home/webadmin/` directory is the main working directory
- Docker volumes are used for persistent data (`coloring_data`)
