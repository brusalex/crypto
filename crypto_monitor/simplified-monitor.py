import asyncio
import aiohttp
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime
import logging
import sys
import time

class CryptoShakeoutMonitor:
    def __init__(self,
                 exchange_id: str = 'binance',
                 check_interval: int = 3600,
                 mode: str = 'TESTING'):
        # Add tracking for last signal to avoid duplicates
        self.last_signal_time = None
        self.last_signal_price = None

        self.timeframes = {
            'SWING_TRADING': {'trend': '1d', 'entry': '4h'},
            'INTRADAY_TRADING': {'trend': '4h', 'entry': '1h'},
            'TESTING': {'trend': '1d', 'entry': '1h'}
        }
        self.current_mode = mode

        # Setup logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('crypto_monitor.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
        if sys.platform.startswith('win'):
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')

        self.logger = logging.getLogger(__name__)
        self.exchange = getattr(ccxt, exchange_id)()
        self.check_interval = check_interval

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Calculate EMA and MACD indicators"""
        try:
            # EMA for trend and value zone
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            df['value_zone_ema'] = df['close'].ewm(span=26, adjust=False).mean()
            
            # MACD
            df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = df['ema12'] - df['ema26']

            return df
        except Exception as e:
            self.logger.error(f'Error calculating indicators: {str(e)}')
            return df

    def analyze_trend(self, symbol: str) -> str:
        """Analyze trend using simple EMA20 slope"""
        try:
            timeframe = self.timeframes[self.current_mode]['trend']
            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=30)  # Reduced to 30 periods
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            
            # Calculate EMA20
            df = self.calculate_indicators(df)
            
            # Simple trend determination
            price_above_ema = df['close'].iloc[-1] > df['ema20'].iloc[-1]
            ema_slope = (df['ema20'].iloc[-1] - df['ema20'].iloc[-5]) / 5  # Using last 5 periods
            
            if price_above_ema and ema_slope > 0:
                self.logger.info("Determined BULLISH trend")
                return 'bullish'
            elif not price_above_ema and ema_slope < 0:
                self.logger.info("Determined BEARISH trend")
                return 'bearish'
                
            self.logger.info("Determined NEUTRAL trend")
            return 'neutral'

        except Exception as e:
            self.logger.error(f'Error in trend analysis: {str(e)}')
            return 'neutral'

    def detect_shakeout(self, df: pd.DataFrame, trend: str) -> dict:
        """
        Detect shakeout pattern:
        - Bullish trend on higher timeframe
        - Price was above value zone (EMA26)
        - Price pulled back to value zone and dropped below
        - MACD is below zero
        """
        try:
            df = self.calculate_indicators(df)
            recent_candles = df.tail(24).copy()  # Last 24 candles for analysis
            last_candle = recent_candles.iloc[-1]

            signal = {
                'is_signal': False,
                'type': None,
                'price': last_candle['close'],
                'ema_value': last_candle['value_zone_ema'],
                'macd_value': last_candle['macd']
            }

            if trend == 'bullish':
                # Check if price was above EMA in recent history
                was_above_ema = any(candle['close'] > candle['value_zone_ema'] 
                                  for _, candle in recent_candles.iterrows())
                
                # Current price below EMA
                current_below_ema = last_candle['close'] < last_candle['value_zone_ema']
                
                # MACD below zero
                macd_below_zero = last_candle['macd'] < 0

                # Log current state
                self.logger.info(f"""
                    Analysis:
                    - Price: {last_candle['close']:.2f}
                    - Value Zone EMA: {last_candle['value_zone_ema']:.2f}
                    - MACD: {last_candle['macd']:.6f}
                    - Was above EMA: {was_above_ema}
                    - Currently below EMA: {current_below_ema}
                    - MACD below zero: {macd_below_zero}
                    """)

                # All conditions met for shakeout
                if was_above_ema and current_below_ema and macd_below_zero:
                    signal['is_signal'] = True
                    signal['type'] = 'LONG'
                    self.logger.info("🎯 Found potential LONG entry point")

            return signal

        except Exception as e:
            self.logger.error(f'Error in shakeout detection: {str(e)}')
            return {'is_signal': False, 'type': None, 'price': 0, 'ema_value': 0, 'macd_value': 0}

    async def run_forever(self, alert_callback=None):
        """Main monitoring loop"""
        self.logger.info('Starting shakeout monitoring...')
        symbol = 'BTC/USDT'

        while True:
            try:
                # 1. Check trend on higher timeframe
                trend = self.analyze_trend(symbol)
                self.logger.info(f'Current {symbol} trend: {trend}')

                if trend == 'neutral':
                    self.logger.info('Neutral trend, skipping analysis')
                    await asyncio.sleep(self.check_interval)
                    continue

                # 2. Analyze entry timeframe
                timeframe = self.timeframes[self.current_mode]['entry']
                ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 3. Look for shakeout
                signal = self.detect_shakeout(df, trend)

                # Check if this is a new signal
                current_time = datetime.now()
                
                # Only send signal if:
                # 1. We haven't sent any signals yet, or
                # 2. It's been at least 4 hours since last signal, or
                # 3. Price has moved at least 2% from last signal
                is_new_signal = (
                    self.last_signal_time is None or
                    (current_time - self.last_signal_time).total_seconds() > 14400 or
                    (self.last_signal_price and 
                     abs(signal['price'] - self.last_signal_price) / self.last_signal_price > 0.02)
                )
                
                if signal['is_signal'] and is_new_signal:
                    # Update last signal tracking
                    self.last_signal_time = current_time
                    self.last_signal_price = signal['price']
                    
                    message = f"""
🚨 Shakeout detected on {symbol}!
📈 Trend: {trend}
💰 Price: {signal['price']:.2f}
📊 Signal Type: {'🟢 LONG' if signal['type'] == 'LONG' else '🔴 SHORT'}
⏰ Time: {datetime.now()}
🔍 MACD: {signal['macd_value']:.6f}
"""
                    if alert_callback:
                        await alert_callback(message)
                        self.logger.info(f'Sent shakeout signal: {signal["type"]}')

                await asyncio.sleep(self.check_interval)

            except Exception as e:
                self.logger.error(f'Error in main loop: {str(e)}')
                await asyncio.sleep(60)

async def send_telegram_alert(message):
    async with aiohttp.ClientSession() as session:
        async with session.post("http://localhost:8000/send_message",
                              json={"text": message}) as response:
            await response.json()

async def main():
    monitor = CryptoShakeoutMonitor(
        exchange_id='binance',
        check_interval=120,
        mode='TESTING'
    )
    await monitor.run_forever(alert_callback=send_telegram_alert)

if __name__ == "__main__":
    asyncio.run(main())