# تشغيل VPS Linux

هذا المشروع مراقب وتحليل فقط. لا يحتوي عميل Capital.com على نقطة إرسال أوامر، ولا يمكن تفعيل التداول الحي أو رفع الرافعة منه.

```bash
sudo useradd --system --home /opt/finbertx --shell /usr/sbin/nologin finbertx
sudo mkdir -p /opt/finbertx/data
sudo chown -R finbertx:finbertx /opt/finbertx
python3 -m venv /opt/finbertx/.venv
/opt/finbertx/.venv/bin/python -m pip install -r /opt/finbertx/requirements.txt
sudo install -o finbertx -g finbertx -m 600 .env /opt/finbertx/.env
sudo install -m 644 deploy/finbertx.service /etc/systemd/system/finbertx.service
sudo systemctl daemon-reload
sudo systemctl enable --now finbertx
sudo journalctl -u finbertx -f
```

ضع المفاتيح على الخادم فقط داخل `.env` بصلاحية `600`. لا ترفعها إلى GitHub. استخدم `/pause` و`/resume` للتحكم في الإشعارات والتحليل، وتحقق من `/status`.
