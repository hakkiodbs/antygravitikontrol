# GitHub Yayın Botu (githubbot)

Tek tıkla: İzlePro APK'larını ve güncelleme dosyalarını GitHub'a (izlepromobil/izlepro-release) yükler.
Bot bu klasörde durur; projeyi `ayarlar.json` içindeki yoldan okur (proje taşınırsa sadece orayı değiştirin).

## Bir kereye mahsus kurulum
1. PowerShell'i açın:  `winget install --id GitHub.cli`   (sonra pencereyi kapatıp yeniden açın)
2. `gh auth login`  ->  GitHub.com > HTTPS > Login with a web browser  (tarayıcıda onaylayın)
   Şifre/anahtar hiçbir dosyaya yazılmaz; giriş GitHub'ın kendi aracında kalır.

## Her yayında
1. Projede sürümü artırın:  `python "C:\Users\MyPC\Desktop\uygulamalar\mobil\izlepro\araclar\surum_yap.py" 2.0.5 7`
2. Android Studio'da Build > Flutter > Build APK yapın.
3. Önce deneme (hiçbir şey göndermez):  `yayinla.bat -Deneme`
4. Her şey yeşilse:  `yayinla.bat`  ve "evet" yazın.
   TV APK'sı da varsa:  `yayinla.bat -TvApk "C:\yol\IzlePro_TV.apk"`

## Bot neleri kontrol eder
- GitHub girişi var mı
- pubspec.yaml, version.json ve indirme adresi aynı sürümü gösteriyor mu
- config.json geçerli mi
- APK koddan eski mi (eskiyse uyarır)
- Bu sürüm zaten yayınlanmış mı (üzerine yazmaz)
- Sıra: önce APK yüklenir, en son version.json gönderilir; "güncelleme var" bildirimi APK hazır olmadan gitmez.
