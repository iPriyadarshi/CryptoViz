# CryptoViz - Cryptocurrency Visualization Platform

![CryptoViz](https://img.shields.io/badge/CryptoViz-Cryptocurrency%20Visualization-blue)
![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Flask](https://img.shields.io/badge/Flask-3.1.0-green)
![JavaScript](https://img.shields.io/badge/JavaScript-ES6-yellow)
![Bootstrap](https://img.shields.io/badge/Bootstrap-5.1.3-purple)
![Chart.js](https://img.shields.io/badge/Chart.js-Latest-orange)

CryptoViz is a cryptocurrency analytics dashboard that brings live prices, historical trends, volatility, correlation and market sentiment for the top cryptocurrencies into a single, interactive interface. It runs as one Flask application that scrapes market data, stores it in a database, and serves both the REST API and the frontend.

![Top Gainers](https://github.com/iPriyadarshi/CryptoViz/blob/main/top-gainers-cryptoviz.png)
## 🚀 Key Features

- **Live Cryptocurrency Price Tracking** - Stay updated with real-time market movements
- **Historical Data Visualization** - Analyze past trends to predict future changes
- **Normalized Trend Analysis** - Compare different assets efficiently
- **Volatility Analysis** - Understand price fluctuations and risk metrics
- **Correlation Matrix** - Discover relationships between different cryptocurrencies
- **Market Sentiment Analysis** - Track market sentiment from various sources
- **Top Gainers Tracking** - Monitor the best performing cryptocurrencies in the last 24 hours
- **User-Friendly Interface** - Designed for accessibility and ease of use

## 📋 Project Structure

The backend and frontend are unified into a single Flask application served from
the repository root. Data is persisted in a database (Turso / libSQL, with a local
SQLite fallback) instead of CSV/JSON files.

```
CryptoViz/
├── app.py                  # Flask app: serves the frontend + REST API
├── database.py             # Database layer (Turso/libSQL, SQLite fallback)
├── scraper.py              # Price & top-gainers scrapers
├── sentiment_scraper.py    # Sentiment scraper (news, Reddit, Fear & Greed)
├── data_utils.py           # Analytics + background update threads
├── historical_scraper.py   # One-off historical price backfill
├── requirements.txt
├── .env.example            # Sample config; copy to .env and fill in (.env is gitignored)
├── static/                 # Frontend assets (css, js, images)
│   ├── css/
│   ├── js/
│   └── assets/images/
└── templates/              # Frontend HTML pages (+ api_docs.html)
```

### Backend (Flask)

The backend provides RESTful API endpoints for:
- Cryptocurrency data retrieval
- Historical price data
- Volatility metrics
- Correlation analysis
- Sentiment data
- Top gainers information

### Frontend (HTML/CSS/JavaScript)

The frontend is built with HTML, CSS, and JavaScript, using:
- Bootstrap for responsive design
- Chart.js for interactive data visualization
- Custom CSS for theming (light/dark mode)

It is served by the same Flask server (same-origin), so there is no separate
frontend server to run.

### Database

All data is stored in a database via `database.py`:
- `crypto_prices` - time-series price rows (history, volatility, correlation)
- `sentiment_snapshots` - one JSON snapshot per sentiment scrape
- `top_gainers_snapshots` - one JSON snapshot per top-gainers scrape

By default it connects to a [Turso](https://turso.tech/) (libSQL / cloud SQLite)
database. If no Turso credentials are configured, it automatically falls back to a
local `crypto.db` SQLite file so the app runs without any cloud setup.

## 🛠️ Installation & Setup

### Prerequisites

- Python 3.9 or higher
- Web browser with JavaScript enabled
- (Optional) A [Turso](https://turso.tech/) database. Without one, the app uses a
  local `crypto.db` SQLite file automatically.

### Setup

1. From the repository root, create and activate a virtual environment (recommended):
   ```bash
   python -m venv venv
   # Windows
   venv\Scripts\activate
   # macOS/Linux
   source venv/bin/activate
   ```

2. Install required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

3. Configure environment variables. Copy `.env.example` to `.env` and (optionally)
   fill in your Turso credentials:
   ```bash
   cp .env.example .env
   ```
   To use Turso, set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN`.
   
   Leave them blank to use the local SQLite fallback.

4. (Optional) Backfill 31 days of historical price data:
   ```bash
   python historical_scraper.py
   ```

5. Start the server:
   ```bash
   python app.py
   ```
   The tables are created automatically on startup. The full application - both
   the website and the API - is available at http://127.0.0.1:5000

## 📊 Available Pages

- **Home** - Overview of top cryptocurrencies with price charts
- **Normalized Trend** - Compare price trends of different cryptocurrencies
- **Volatility** - Analyze price volatility metrics
- **Correlation** - View correlation between different cryptocurrencies
- **Sentiment** - Track market sentiment from news and social sources
- **Top Gainers** - Monitor the best performing cryptocurrencies
- **Website Info** - How the platform works and where the data comes from
- **About** - The team behind CryptoViz

## 🔌 API Endpoints

The backend provides the following API endpoints:

- `GET /api/crypto` - Latest data for all tracked cryptocurrencies
- `GET /api/crypto/{symbol}/history` - Historical price data for a specific cryptocurrency
- `GET /api/crypto/{symbol}/volatility` - Volatility metrics for a specific cryptocurrency
- `GET /api/crypto/volatility` - Volatility metrics for all cryptocurrencies
- `GET /api/crypto/correlation` - Correlation matrix for all cryptocurrencies
- `GET /api/sentiment` - Latest complete sentiment data
- `GET /api/sentiment/overall` - Overall market sentiment (supports `?days=1|7|30`)
- `GET /api/sentiment/rankings` - Most positive / negative cryptocurrencies by sentiment
- `GET /api/sentiment/sources` - Raw sentiment sources (filter by `type`, `crypto`, `limit`, `days`)
- `GET /api/sentiment/trends` - Sentiment and price trend over time
- `GET /api/top-gainers` - Latest top gaining cryptocurrencies
- `GET /api/top-gainers/update` - Trigger a top gainers data update
- `GET /api/sentiment/update` - Trigger a sentiment data update

An HTML summary of these endpoints is also available at `/api-docs`.

## 🔄 Data Sources

CryptoViz collects data from multiple sources:

- **Price data**: CoinMarketCap (primary), CoinGecko API (fallback)
- **Top gainers**: CoinMarketCap (primary), CoinGecko API (fallback)
- **Sentiment**: crypto news sites and Reddit (r/CryptoCurrency) scored with NLTK's VADER model, combined with the Crypto Fear &amp; Greed Index (weighted 50% news, 30% social, 20% index)

## 🎨 Customization

### Themes

CryptoViz supports both light and dark themes. You can toggle between themes using the theme switcher in the application header.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

## 📝 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 📧 Contact

For questions or feedback, please open an issue on GitHub.

---

Built with ❤️ for cryptocurrency enthusiasts and data visualization lovers.
