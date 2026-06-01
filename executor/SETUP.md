# Kalshi API Key Setup — Step by Step

## Demo Environment (start here)

1. Go to **https://demo.kalshi.co** in your browser
2. Click **Sign Up** and create an account
   - Use any email (doesn't need to match your real Kalshi account)
   - Set a password
   - You do NOT need to verify identity for demo
3. Once logged in, click your **profile icon** (top right) → **Settings**
4. Click **API Keys** in the left sidebar
5. Click **Generate New Key**
6. You'll see two things:
   - **Key ID** — a string like `abc123-def456-ghi789` (copy this)
   - **Download Private Key** button — click it to download a `.pem` file
7. **IMPORTANT**: Save the `.pem` file immediately. Kalshi will NOT show it again.
8. Move the `.pem` file into this directory:
   ```
   mv ~/Downloads/kalshi_private_key.pem ./kalshi_private_key.pem
   ```
9. Add demo funds:
   - Go to **Settings → Wallet → Deposit**
   - Use the test payment method to add $5,000+ in demo funds
10. Create your `.env`:
    ```
    cp .env.example .env
    ```
11. Fill in your `.env`:
    ```
    KALSHI_KEY_ID=abc123-def456-ghi789
    KALSHI_PRIVATE_KEY_PATH=./kalshi_private_key.pem
    KALSHI_ENV=demo
    ```
12. Start the executor:
    ```
    pip install -r requirements.txt
    python server.py
    ```
    You should see: `Connected to Kalshi demo API — balance: $5000.00`

## Production (switch when ready for real money)

1. Go to **https://kalshi.com** and log in (or create account)
2. Go to **Settings → Profile → Verify Identity** (required for trading)
3. Go to **Settings → API Keys → Generate New Key**
4. Download the PEM file (save as `kalshi_prod_key.pem`)
5. Deposit real funds: **Settings → Wallet → Deposit**
6. Update your `.env`:
   ```
   KALSHI_KEY_ID=your-production-key-id
   KALSHI_PRIVATE_KEY_PATH=./kalshi_prod_key.pem
   KALSHI_ENV=production
   ```
7. Restart the executor
