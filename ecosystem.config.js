// สูตร deploy สำหรับ PM2 — ใช้รันบอทเฮียตี๋ค้างไว้บน VPS
//
// วิธีใช้บน VPS:
//   cd /root/heretee168
//   python3 -m venv venv && ./venv/bin/pip install -r requirements.txt
//   pm2 start ecosystem.config.js
//   pm2 logs heretee-bot        # ดู log
//   pm2 restart heretee-bot     # รีสตาร์ทหลัง git pull
//   pm2 save && pm2 startup     # ให้บอทขึ้นเองหลังรีบูตเครื่อง
//
// ความปลอดภัย: ไฟล์นี้อยู่ใน git จึงห้ามใส่ token / API key ใด ๆ ที่นี่เด็ดขาด
// ความลับทุกตัว (TELEGRAM_BOT_TOKEN, ANTHROPIC_API_KEY, API_FOOTBALL_KEY, GROUP_CHAT_ID ฯลฯ)
// อยู่ในไฟล์ .env ซึ่งไม่ถูก commit และ bot.py โหลดเองด้วย python-dotenv

module.exports = {
  apps: [
    {
      name: 'heretee-bot',
      script: 'src/bot.py',

      // ใช้ python ใน venv (ไม่ใช่ python ของระบบ) จะได้เห็นไลบรารีที่ pip ติดตั้งไว้
      interpreter: '/root/heretee168/venv/bin/python3',

      // working directory — ให้ dotenv หา .env เจอ และ path สัมพัทธ์เดิมทำงานได้เหมือนรันเอง
      cwd: '/root/heretee168',

      // บอทเป็น long polling ตัวเดียว ห้ามรันหลาย instance (จะแย่ง getUpdates กัน)
      instances: 1,
      exec_mode: 'fork',

      autorestart: true,
      max_restarts: 10, // พังซ้ำเกินนี้ให้หยุด จะได้รู้ว่าเสียถาวร ไม่ใช่วน restart ไม่จบ
      restart_delay: 5000, // รอ 5 วินาทีก่อน restart กัน restart รัว ๆ
      max_memory_restart: '300M', // กิน RAM เกินนี้ให้ restart กัน memory leak

      // ไม่ต้องดู log สด ๆ ก็ปิด watch ไว้ (แก้โค้ดแล้วค่อย pm2 restart เอง)
      watch: false,
    },
  ],
};
