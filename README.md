# Antigravity ⇄ Telegram Köprüsü

Antigravity (Gemini tabanlı IDE / Agent Manager) ve **Claude Code** için Telegram uzaktan kumandası.
Bilgisayarından uzaktayken: işler bitince haber alır, cevapları çeker, sohbetlere mesaj/dosya/sesli not gönderirsin.

*English: a Telegram remote control for Antigravity (via Chrome DevTools Protocol) plus a hook for Claude Code. Notifies when jobs finish, lets you browse projects/conversations, send messages and files, approve permission prompts, and pull files back. Single Python file, no cloud service, only your own bot and your own Telegram account can control it.*

## Özellikler
- ✅ Her proje/sohbet için "iş bitti" bildirimi (özet + dosya düğmeleri), birden fazla iş aynı anda
- 📁 Telegram'dan proje → sohbet seç, yeni sohbet aç, mesaj yaz (yanlış sohbete yazmayı önlemek için başlık doğrulaması)
- 📎 Fotoğraf/PDF/Excel/Word/video gönder → sohbete gerçek ek olarak eklenir (olmazsa dosya yolu yazılır)
- ⏸ Ajan onay beklerse haber verir, düğmeyle onaylarsın · ⚠️ takılma / uzun süre uyarısı
- 🎙 Sesli mesaj → yazıya çevrilir → onaylarsan gönderilir
- ⏰ `/zamanla 03:00 mesaj` · 📤 `/dosya yol` · 🖥 `/pc` · 🔄 `/yenidenbaslat`
- 📲 Telegram'a sığmayan büyük dosyalar (APK vb.) doğrudan **WhatsApp'a** gönderilir: `/wa yol` ya da dosya altındaki 📲 düğmesi
- 🔐 Sadece eşleştirdiğin Telegram hesabı kullanabilir; yetkisiz deneme uyarısı; tehlikeli komutlarda onay
- Claude Code için: iş bitince / izin istenince Telegram bildirimi (`claude_hook.py`)

## Kurulum (Windows)
1. [@BotFather](https://t.me/BotFather) ile kendi botunu oluştur, token'ı al.
2. Python 3.9+ kur. Bu klasörde `Kurulum.bat` çalıştır, token'ı yapıştır.
3. Ekrandaki 6 haneli kodu botuna Telegram'dan yaz (eşleşme).
4. Antigravity'yi masaüstündeki **"Antigravity (Telegram)"** kısayolundan aç (köprü portu ile açılır).
5. Telegram'da `/start`. Güncellemeden sonra `Kopru-Yeniden-Baslat.bat`.

Ses için: `Ses-Kurulum.bat` (yerel, ücretsiz) ya da `OPENAI_API_KEY` ayarı.

## WhatsApp'a dosya gönderme (isteğe bağlı)
Telegram bot sınırı 50 MB'dır; büyük dosyaları köprü kendi WhatsApp Web penceresinden gönderir (kendi hesabınla, resmi API gerekmez).
1. Telegram'da `/whatsapp` yaz → ekrana gelen QR kodu telefonundan okut (WhatsApp → Bağlı cihazlar). Bir kez yapılır, oturum `whatsapp-profil` klasöründe kalır.
2. Kişi ekle: `/wakisi ben 905551112233` (kendin), `/wakisi ali 905441112233` (ülke koduyla, başında + yok).
3. `/wa C:\yol\uygulama.apk` ya da dosya düğmelerindeki **📲 WhatsApp** → kişiyi seç → gönderilir.
- Ayrı bir Chrome/Edge penceresi açılır; kapatma. Resmi olmayan otomasyondur; WhatsApp kullanım şartları buna karşıdır, düşük hacimli kişisel kullanımda risk küçüktür ama sıfır değildir. `.exe/.bat` gibi türler otomatik zip'lenir.
- `whatsapp-profil/` klasörü oturum bilgisi içerir: `.gitignore` bunu dışarıda tutar, `Token-Sifirla.bat` siler.

## Kendi tokenin, ayarlar ve paylaşmadan önce temizlik
- **Her kullanıcı kendi botunu ve tokenini kullanır.** `Kurulum.bat` token'ı sorar ve doğrular; sonradan değiştirmek için `Ayarlar.bat` (token değiştir, eşleşmeyi sıfırla).
- **GitHub'a yüklemeden önce `Token-Sifirla.bat`** çalıştır (en iyisi klasörün bir kopyasında): token, eşleşme, günlükler, gelen dosyalar, zamanlamalar ve otomatik başlatma kaydı silinir. Sonra BotFather'da `/revoke` yap ki eski token tamamen geçersiz olsun.
- Bu işlemi kendi çalışan kurulumunda yaparsan botun durur; tekrar `Kurulum.bat` gerekir.

## Claude Code bildirimleri
`~/.claude/settings.json` (Windows: `C:\Users\ADIN\.claude\settings.json`) içine ekle:
```json
{
  "hooks": {
    "Stop":         [{ "hooks": [{ "type": "command", "command": "python C:\\yol\\claude_hook.py" }] }],
    "Notification": [{ "hooks": [{ "type": "command", "command": "python C:\\yol\\claude_hook.py" }] }]
  }
}
```
Aynı klasördeki `ayarlar.env` (token + kullanıcı no) kullanılır; köprünün çalışması gerekmez.

## Güvenlik
- Token'ı ve `ayarlar.env` dosyasını **asla paylaşma / GitHub'a yükleme** (`.gitignore` bunu engeller). Sızarsa BotFather'da `/revoke`.
- Bot yalnızca eşleştirilen kullanıcı numarasını dinler. Antigravity "Turbo" modundaysa uzaktan verilen komutlar bilgisayarında çalışır: bunu bilerek kullan.
- `/dosya` yalnızca izinli klasörlerden (`DOSYA_KOKLERI`, varsayılan kullanıcı klasörü) dosya yollar; `.ssh`, `.env`, tarayıcı verisi vb. engellidir.

## Sınırlar
- Antigravity'nin arayüzünü okur; arayüz güncellenirse bazı özellikler bozulabilir. Sorun olursa Telegram'da `/teshis` yaz, `teshis.json` ile issue aç (token içermez).
- Telegram bot sınırı: gelen dosya ≤ 20 MB, giden ≤ 50 MB.

Lisans: MIT
