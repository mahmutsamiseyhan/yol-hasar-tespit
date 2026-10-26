#!/usr/bin/env bash
# Ubuntu 22.04/24.04 sunucuya kurulum. Sunucuda root olarak çalıştırılır; tekrar çalıştırmak güvenlidir (günceller).
#   DOMAIN=ornek.com bash sunucu_kurulum.sh
# Önce model dosyası kopyalanmış olmalı: /opt/yol-hasar-tespit/models/best.pt
set -euo pipefail

REPO="${REPO:-https://github.com/mahmutsamiseyhan/yol-hasar-tespit.git}"
DIR=/opt/yol-hasar-tespit
DOMAIN="${DOMAIN:?DOMAIN gerekli, ör. DOMAIN=1-2-3-4.sslip.io}"

echo "==> Paketler (Docker, git, güvenlik duvarı)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2 git ufw >/dev/null
systemctl enable --now docker >/dev/null

# PyTorch + model ~1.5 GB RAM kullanır; küçük sunucuda bellek taşmasın diye takas alanı
if [ "$(free -m | awk '/Mem:/{print $2}')" -lt 3500 ] && ! swapon --show | grep -q /swapfile; then
  echo "==> 2 GB takas alanı ekleniyor (RAM 4 GB'tan az)"
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap -q /swapfile && swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "==> Güvenlik duvarı: yalnızca SSH, HTTP, HTTPS"
ufw allow OpenSSH >/dev/null && ufw allow 80/tcp >/dev/null && ufw allow 443/tcp >/dev/null && ufw allow 443/udp >/dev/null
ufw --force enable >/dev/null

echo "==> Kod"
if [ -d "$DIR/.git" ]; then git -C "$DIR" pull -q; else git clone -q "$REPO" "$DIR"; fi
mkdir -p "$DIR/models"
[ -f "$DIR/models/best.pt" ] || { echo "HATA: $DIR/models/best.pt yok. Önce model dosyasını kopyalayın."; exit 1; }
echo "DOMAIN=$DOMAIN" > "$DIR/deploy/.env"

echo "==> Uygulama ve HTTPS başlatılıyor (ilk derleme birkaç dakika sürer)"
cd "$DIR/deploy"
docker compose -f docker-compose.prod.yml up -d --build

echo "==> Sağlık kontrolü"
for _ in $(seq 1 60); do
  if curl -fsS -o /dev/null "https://$DOMAIN/health"; then echo "Hazır: https://$DOMAIN"; exit 0; fi
  sleep 5
done
echo "Uygulama henüz yanıt vermiyor. Kayıtlar: docker compose -f $DIR/deploy/docker-compose.prod.yml logs --tail 50"
exit 1
