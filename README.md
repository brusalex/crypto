# Elder Triple Screen Trading Strategy

## Overview

This project implements Dr. Alexander Elder's Triple Screen trading strategy for cryptocurrency markets. The system automates trading decisions by analyzing multiple timeframes using Elder's three-filter approach to identify high-probability trading opportunities.

## Features

- **Multi-timeframe Analysis**: Analyzes trend, entry points, and confirmation using three different timeframes
- **Dual Trading Modes**: Supports both intraday and swing trading modes simultaneously
- **Real-time Alerts**: Sends trading signals to Telegram when opportunities are detected
- **Risk Management**: Implements stop-loss, take-profit, and trailing stop mechanisms
- **Docker Deployment**: Easy containerized deployment using Docker Compose
- **Modular Architecture**: Cleanly separated components for signal generation and notifications

## System Architecture

The system consists of two main components:

1. **Crypto Monitor**: Implements the Elder Triple Screen strategy to analyze market data and generate signals
2. **Telegram Service**: Sends alerts to a Telegram bot when trading signals are detected

## Strategy Details

### Elder Triple Screen Strategy

The strategy uses three "screens" (filters) across multiple timeframes to identify and confirm trading opportunities:

1. **First Filter: Trend Direction (Higher Timeframe)**
   - Uses MACD histogram direction on weekly/daily chart
   - Bullish when MACD histogram is rising
   - Bearish when MACD histogram is falling

2. **Second Filter: Market Wave (Middle Timeframe)**
   - Uses oscillators to find counter-trend moves
   - For bullish trends: Buys when Force Index drops below zero and Stochastic is below 30
   - For bearish trends: Shorts when Force Index rises above zero and Stochastic is above 70

3. **Third Filter: Entry Timing (Shorter Timeframe)**
   - Uses price crossing 5-period EMA on the shortest timeframe
   - For LONG entries: Price crossing above the EMA
   - For SHORT entries: Price crossing below the EMA

## Installation & Setup

### Prerequisites

- Docker and Docker Compose
- Telegram bot token (for alerts)

### Environment Setup

1. Create a `.env` file in the project root with the following variables:
   ```
   TELEGRAM_TOKEN=your_telegram_bot_token
   DEFAULT_CHAT_ID=your_telegram_chat_id
   ```

### Running the System

1. Build and start the services:
   ```bash
   docker-compose up -d
   ```

2. Check logs to monitor the strategy:
   ```bash
   docker-compose logs -f elder-strategy
   ```

## Configuration Options

The strategy can be customized by editing the `ElderStrategyManager` parameters in `eldar_strategy_filters.py`:

```python
manager = ElderStrategyManager(
    exchange_id='binance',     # Exchange to use
    symbol='BTC/USDT',         # Trading pair
    intraday_interval=1200,    # Check interval for intraday (seconds)
    swing_interval=3600,       # Check interval for swing trading (seconds)
    run_intraday=True,         # Enable/disable intraday strategy
    run_swing=True             # Enable/disable swing strategy
)
```

### Timeframe Configuration

Timeframes for each mode can be configured in the `ElderTripleScreenStrategy` class:

```python
self.timeframes = {
    'SWING': {'trend': '1w', 'entry': '1d', 'intraday': '1h'},
    'INTRADAY': {'trend': '1d', 'entry': '1h', 'intraday': '15m'}
}
```

## Development

### Project Structure

```
crypto_monitor/
  ├── eldar_strategy_filters.py  # Main strategy implementation
  ├── Dockerfile                 # Dockerfile for the strategy
  └── requirements.txt           # Python dependencies

telegram_service/
  ├── app.py                     # FastAPI service for Telegram alerts
  ├── Dockerfile                 # Dockerfile for the Telegram service
  └── requirements.txt           # Python dependencies

docker-compose.yml               # Docker Compose configuration
```

## Troubleshooting

### Common Issues

1. **No signals generated**: 
   - Verify that the exchange API is working correctly
   - Check if the specified trading pair has sufficient volume and data
   - Ensure timeframes are properly configured for the instrument

2. **Telegram alerts not working**:
   - Verify the TELEGRAM_TOKEN and DEFAULT_CHAT_ID in the .env file
   - Check telegram-bot service logs: `docker-compose logs telegram-bot`

## Risk Disclaimer

This software is for educational and informational purposes only. Trading cryptocurrencies involves substantial risk of loss and is not suitable for every investor. The developers of this software are not responsible for any financial losses incurred while using this system.

## License

This project is licensed under the MIT License - see the LICENSE file for details.