# 🚀 Crypto Breakout Scanner — Backend

幣安合約橫盤縮量爆發偵測 API，部署在 Railway。

## 本機測試

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

打開 http://localhost:8000 應該會看到 API 說明頁。

## 部署到 Railway（完整步驟見對話中的教學）

1. 把這個資料夾推上 GitHub
2. 到 https://railway.app 用 GitHub 登入
3. New Project → Deploy from GitHub repo → 選這個 repo
4. Railway 會自動偵測 `requirements.txt` 並部署
5. Settings → Networking → Generate Domain，拿到網址
6. 把網址貼到前端 Artifact 的設定欄

## API 端點

- `GET /scan?symbols=BTCUSDT,ETHUSDT&interval=4h` — 掃描指定幣種
- `GET /health` — 健康檢查
- `GET /docs` — Swagger 文件
