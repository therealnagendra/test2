# Smart Parking (live version) - Flask + SQLite + Google Maps + UPI

## Run on laptop
    pip install -r requirements.txt
    set GOOGLE_MAPS_API_KEY=your_key      (Windows)   |   export GOOGLE_MAPS_API_KEY=your_key  (Linux/Mac)
    python app.py        ->  open http://localhost:5000
Without a key the app uses OpenStreetMap automatically (free). With the key it uses Google Maps.

## Google Maps key (5 min)
1. console.cloud.google.com -> new project -> enable "Maps JavaScript API" and "Geocoding API"
2. Credentials -> Create API key -> restrict it to your website URL
3. Billing account is required by Google (there is a free monthly credit).

## Put it online (free): render.com
1. Upload this folder to GitHub  2. Render -> New Web Service -> connect repo
3. Build: pip install -r requirements.txt   Start: gunicorn app:app
4. Environment: GOOGLE_MAPS_API_KEY, SECRET_KEY (any long random text), UPI_ID=7416779528@Upi, HELP_NUMBER=7416779528
5. GPS needs HTTPS - Render gives it automatically.
Note: free hosts reset SQLite on redeploy; for permanent data use a Render disk (set DB_PATH) or MySQL.

## Payment (YouPI PG)
Customer taps "Pay with UPI" -> YouPI checkout opens (mobile verify, QR / GPay / PhonePe / Paytm) -> bank SMS is observed
-> YouPI sends a signed webhook to /api/youpi-webhook -> booking becomes Confirmed automatically (no UTR typing).
The app also asks YouPI directly (GET /api/v1/payments/<id>) if the webhook is late, and checks amount + order id.

Set these environment variables (Render -> Environment):
    YOUPI_API_URL=https://pay.yourdomain.com      (your YouPI payment-api, server-to-server)
    YOUPI_MERCHANT_ID=<merchant uuid from YouPI merchant admin>
    YOUPI_WEBHOOK_SECRET=<merchant webhook secret>
    PUBLIC_URL=https://your-parking-app.onrender.com
In the YouPI merchant admin set the webhook URL to:  <PUBLIC_URL>/api/youpi-webhook

Run YouPI itself (Node 24+): unzip YouPI-PG, `npm install`, `npm run db:setup`, `npm start` (see its docs/INSTALLATION.md,
OBSERVERS_SETUP.md). It needs a public HTTPS host, a WhatsApp number for OTP and the Android observer app on the phone
that receives your bank SMS.
If the three YOUPI_* variables are NOT set, the app falls back to the old manual flow: dynamic UPI QR + 12-digit UTR + owner confirm.

YouPI is an experimental research project (not NPCI/bank approved). Keep to its limit of ~20 payments/day per account and use
it only for demo/college use, not real business money.

## Easy start
Windows: double-click run.bat (needs Python from python.org, tick "Add Python to PATH"), then open http://localhost:5000
Mac/Linux: sh run.sh, then open http://localhost:5001
Mobile phone: this cannot run on a phone directly - deploy on render.com (steps above) and open the link.
