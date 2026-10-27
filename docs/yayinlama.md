# Yayınlama: kalıcı HTTPS adresi

Amaç: uygulama her zaman açık olsun, telefonla her yerden `https://...` adresinden açılsın (telefonlar kamera ve konum iznini yalnızca HTTPS'te verir).

Kurulum: **Docker** (uygulama) + **Caddy** (otomatik HTTPS, Let's Encrypt). Dosyalar: `deploy/`.

## 1. Sunucu kiralama
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
- **Ücretsiz, hemen:** `IP-adresi.sslip.io` (ör. IP `185.10.20.30` ise `185-10-20-30.sslip.io`). Ek ayar gerekmez. Alan adı hazır olana kadar bununla başlanır.
- **Kendi alan adınız** (ör. `yolhasari.com.tr`): alan adının sunucu IP'sini gösteren bir **A kaydı** olmalı.

### Alan adı panelinde yalnızca "Name Server" ayarı varsa (A kaydı eklenemiyorsa)
DNS kayıtlarını ücretsiz **Cloudflare DNS** ile yönetin:
1. https://dash.cloudflare.com adresinde ücretsiz hesap açın → **Add a domain** → alan adını yazın → **Free** planı seçin.
2. **DNS → Records** bölümünde iki kayıt ekleyin. Proxy durumu **DNS only (gri bulut)** olsun; HTTPS sertifikasını sunucudaki Caddy alır.

   | Type | Name | IPv4 address | Proxy status |
   |---|---|---|---|
   | A | `@` | sunucu IP'si | DNS only |
   | A | `www` | sunucu IP'si | DNS only |
3. Cloudflare size iki name server verir (ör. `xxx.ns.cloudflare.com`, `yyy.ns.cloudflare.com`). Alan adı panelinde **Name Server Güncelle** bölümüne bu ikisini yazın (eski `ns1/ns2` yerine), **NS Güncelle**.
4. `.tr` alan adlarında kayıt sisteminin (TRABIS) değişikliği işlemesi birkaç saat sürebilir. Cloudflare alan adını "Active" gösterdiğinde hazırdır.

Alternatif: Alan adı firmasının destek hattından kendi DNS sunucularında alan adınız için bir A kaydı açmalarını isteyin.

## 4. Kurulum (benim adımım)
Sunucu IP'sini ve seçtiğiniz adresi verdiğinizde:
1. Model dosyasını sunucuya kopyalarım: `models/best.pt` → `/opt/yol-hasar-tespit/models/`
2. Kurulum betiğini çalıştırırım: `DOMAIN=... bash deploy/sunucu_kurulum.sh`
   - Docker'ı kurar, güvenlik duvarında yalnızca SSH/HTTP/HTTPS'i açar, gerekirse takas alanı ekler
   - Kodu GitHub'dan çeker, uygulamayı ve HTTPS'i başlatır, sağlık kontrolü yapar
   - İlk kurulumda yönetici şifresi ve oturum anahtarı üretir, sunucudaki `deploy/.env` dosyasına yazar (yalnızca root okuyabilir). Şifre olmadan uygulama başlamaz.
3. Telefonla `https://ADRES/bildir` adresini deneriz; panel `https://ADRES` adresinde, kullanıcı `yonetici`.
   Şifreyi sunucuda görmek için: `sudo grep ADMIN_PASSWORD /opt/yol-hasar-tespit/deploy/.env`. Değiştirmek için o satırı düzenleyip betiği tekrar çalıştırın.

Güncelleme: kod değişince aynı betiği tekrar çalıştırmak yeterli.

## Güvenlik ve KVKK notları
- İş takip paneli, arıza listesi, görseller ve durum değişiklikleri **yönetici girişi** ister. Herkese açık olanlar yalnızca bildirim sayfası (`/bildir`) ve bildirene özel takip kodu ile takip sayfası (`/takip`).
- Oturum imzalı bir çerezle tutulur (HttpOnly, Secure, SameSite=Strict). Giriş denemesi IP başına dakikada 10 ile sınırlıdır; başarısız denemeler kayda geçer. Durum değişikliklerinde işlemi yapan kullanıcı ve zaman saklanır.
- Kötüye kullanımı sınırlamak için IP başına dakikada 20 fotoğraf / 60 durum değişikliği / 30 takip sorgusu sınırı vardır. Etiket aracı yayında kapalıdır.
- Birden çok personel hesabı için `deploy/.env` içine `ADMIN_USERS='ali:sifre1,veli:sifre2'` eklenebilir. Şifre yerine özet de yazılabilir: `python -m app.auth`.
- Fotoğraflardaki yüzler ve plakalar kaydedilmeden önce bulanıklaştırılır; ham görüntü saklanmaz. Veri Türkiye'deki sunucudadır.
- Bu bir demo kurulumudur; gerçek vatandaş verisi için belediyenin kendi altyapısı, kurumsal kimlik doğrulama (ör. e-Devlet / belediye dizini) ve KVKK aydınlatma metni gerekir.
- İş bitince sunucuyu panelden kapatın / silin.
