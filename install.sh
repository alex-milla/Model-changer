#!/bin/bash
# Instalador rápido para Ubuntu 24.04 del Model Changer.
set -e

PROJECT_DIR="/opt/model-changer"
SERVICE_USER="${SUDO_USER:-$USER}"

if [ "$EUID" -ne 0 ]; then
    echo "Por favor ejecuta este script con sudo: sudo ./install.sh"
    exit 1
fi

echo "==> Instalando Model Changer en $PROJECT_DIR"

# Si no estamos ya en el directorio del proyecto, copiamos los archivos
if [ "$(pwd)" != "$PROJECT_DIR" ]; then
    mkdir -p "$PROJECT_DIR"
    cp -r . "$PROJECT_DIR"
fi

cd "$PROJECT_DIR"

# Asegurar que el proyecto pertenece al usuario que ejecutará el servicio
chown -R "$SERVICE_USER":"$SERVICE_USER" "$PROJECT_DIR"

echo "==> Instalando dependencias del sistema"
apt-get update
apt-get install -y python3-venv python3-pip

echo "==> Creando entorno virtual de Python"
if [ -d "venv" ]; then
    rm -rf venv
fi
sudo -u "$SERVICE_USER" python3 -m venv venv
sudo -u "$SERVICE_USER" venv/bin/pip install --upgrade pip
sudo -u "$SERVICE_USER" venv/bin/pip install -r requirements.txt

echo "==> Preparando directorios"
sudo -u "$SERVICE_USER" mkdir -p logs

echo "==> Instalando servicio systemd"
# Ajustar el usuario del servicio al usuario real (tú)
sed -i "s/^User=.*/User=$SERVICE_USER/" model-changer.service
sed -i "s/^Group=.*/Group=$SERVICE_USER/" model-changer.service
cp model-changer.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable model-changer

echo ""
echo "===================================================="
echo " Instalación completa."
echo " 1. Edita la configuración: $PROJECT_DIR/config.yaml"
echo " 2. Inicia el servicio:     sudo systemctl start model-changer"
echo " 3. Abre el panel:          http://$(hostname -I | awk '{print $1}'):8081"
echo "===================================================="
