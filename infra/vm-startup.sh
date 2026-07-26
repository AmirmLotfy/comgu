#!/bin/bash
# Comgu DataHub host bootstrap. Runs as root on first boot.
set -eux
export DEBIAN_FRONTEND=noninteractive

apt-get update
apt-get install -y ca-certificates curl gnupg git jq python3-pip python3-venv unzip

# --- Docker (official repo) ---
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker

# --- Swap (DataHub quickstart recommends >=2G) ---
if [ ! -f /swapfile ]; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

# --- Raise limits Elasticsearch needs ---
sysctl -w vm.max_map_count=262144
echo 'vm.max_map_count=262144' > /etc/sysctl.d/99-comgu.conf

# --- Anyone who logs in should be able to use docker ---
cat > /etc/profile.d/comgu-docker.sh <<'EOF'
if id -nG "$USER" 2>/dev/null | grep -qvw docker; then
  sudo usermod -aG docker "$USER" 2>/dev/null || true
fi
EOF
chmod 644 /etc/profile.d/comgu-docker.sh

touch /var/log/comgu-startup-done
echo "comgu bootstrap complete" > /var/log/comgu-startup.log
