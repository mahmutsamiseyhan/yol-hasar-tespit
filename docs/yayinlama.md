# Yayınlama: Türkiye'deki bir sunucuda kalıcı HTTPS adresi

Amaç: uygulama her zaman açık olsun, telefonla her yerden `https://...` adresinden açılsın (telefonlar kamera ve konum iznini yalnızca HTTPS'te verir). Veri Türkiye'deki sunucuda kalır.

Kurulum: **Docker** (uygulama) + **Caddy** (otomatik HTTPS, Let's Encrypt). Dosyalar: `deploy/`.

## 1. Sunucuyu kiralayın (sizin adımınız)
Örnek sağlayıcı: TuemCloud bulut sunucu. Seçimler:

| Ayar | Öneri |
|---|---|
| İşletim sistemi | **Ubuntu 24.04 LTS** (veya 22.04) |
| RAM | **4 GB** önerilir. 2 GB'ta kurulum betiği takas alanı ekler; çalışır ama ilk analiz yavaşlar. |
| CPU | 2 vCPU |
| Disk | 30 GB yeterli (Docker imajı ~2,6 GB) |
| IP | Sabit IPv4 |

Sunucu açılınca panelde **IP adresini** ve **root şifresini** görürsünüz. **Şifreyi kimseyle paylaşmayın.**

## 2. Kurulum anahtarını sunucuya ekleyin (sizin adımınız)
Kurulumu şifresiz, SSH anahtarıyla yapacağım. İki yoldan biri:

**a) Panelde "SSH anahtarı" alanı varsa** şu satırı yapıştırın:
```
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH5B+5iUvTgC8kOmWcRn2FSKD5p6zQudEcgS9rc/Ao6t yol-hasar-tespit-deploy
```

**b) Yoksa** panelin web konsolundan (veya kendi bilgisayarınızdan `ssh root@IP`) root olarak girip şu komutu çalıştırın:
```bash
mkdir -p ~/.ssh && echo 'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIH5B+5iUvTgC8kOmWcRn2FSKD5p6zQudEcgS9rc/Ao6t yol-hasar-tespit-deploy' >> ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
```

İşiniz bitince bu anahtarı silerek erişimi kapatabilirsiniz (`~/.ssh/authorized_keys` dosyasından ilgili satır).

## 3. Adres (alan adı)
- **Ücretsiz, hemen:** `IP-adresi.sslip.io` (ör. IP `185.10.20.30` ise `185-10-20-30.sslip.io`). Ek ayar gerekmez.
- **Daha profesyonel:** kendi alan adınız (ör. `yolhasari.com`). Alan adı panelinde bir **A kaydı** ile sunucu IP'sine yönlendirin.

## 4. Kurulum (benim adımım)
Sunucu IP'sini ve seçtiğiniz adresi verdiğinizde:
1. Model dosyasını sunucuya kopyalarım: `models/best.pt` → `/opt/yol-hasar-tespit/models/`
2. Kurulum betiğini çalıştırırım: `DOMAIN=... bash deploy/sunucu_kurulum.sh`
   - Docker'ı kurar, güvenlik duvarında yalnızca SSH/HTTP/HTTPS'i açar, gerekirse takas alanı ekler
   - Kodu GitHub'dan çeker, uygulamayı ve HTTPS'i başlatır, sağlık kontrolü yapar
3. Telefonla `https://ADRES/bildir` adresini deneriz.

Güncelleme: kod değişince aynı betiği tekrar çalıştırmak yeterli.

## Güvenlik ve KVKK notları
- Uygulamada kullanıcı girişi yok: adresi bilen herkes bildirim gönderebilir ve kayıt durumunu değiştirebilir. Kötüye kullanımı sınırlamak için IP başına dakikada 20 fotoğraf / 60 durum değişikliği sınırı vardır. Etiket aracı yayında kapalıdır.
- Fotoğraflardaki yüzler ve plakalar kaydedilmeden önce bulanıklaştırılır; ham görüntü saklanmaz. Veri Türkiye'deki sunucudadır.
- Bu bir demo kurulumudur; gerçek vatandaş verisi için belediyenin kendi altyapısı, kullanıcı girişi ve KVKK aydınlatma metni gerekir.
- İş bitince sunucuyu panelden kapatın / silin.
