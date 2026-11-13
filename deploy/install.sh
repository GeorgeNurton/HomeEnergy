#!/bin/sh
# Sets HomeEnergy up on a Raspberry Pi. Run from the HomeEnergy folder:   sh deploy/install.sh
# Safe to run again (after an update, say): it never overwrites your config.toml or secrets.toml.
set -e
cd "$(dirname "$0")/.."
DIR="$(pwd)"
USER_NAME="$(id -un)"

python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || {
  echo "Needs Python 3.11 or newer (Raspberry Pi OS Bookworm or later)."; exit 1; }

for f in config secrets; do
  if [ ! -f "$f.toml" ]; then
    cp "$f.example.toml" "$f.toml"
    echo "Made $f.toml from the example - fill it in (nano $f.toml)."
  fi
done
chmod 600 secrets.toml

sed -e "s#User=USER#User=$USER_NAME#" -e "s#/home/USER/homeenergy#$DIR#" \
    deploy/homeenergy.service > /tmp/homeenergy.service
echo "Installing the service (asks for your password)..."
sudo cp /tmp/homeenergy.service /etc/systemd/system/homeenergy.service
sudo systemctl daemon-reload
sudo systemctl enable homeenergy

echo
echo "Done. Next:"
echo "  1. fill in config.toml and secrets.toml          (nano config.toml)"
echo "  2. check everything, read-only:                    python3 -m homeenergy.check"
echo "  3. start it:                                       sudo systemctl restart homeenergy"
echo "  4. on a phone on the home WiFi:                    http://$(hostname -I | cut -d' ' -f1)"
