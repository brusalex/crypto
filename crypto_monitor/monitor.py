import asyncio
import aiohttp
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Tuple
import time
import sys


class CryptoShakeoutMonitor:
    def __init__(self,
                 exchange_id: str = 'binance',
                 min_volume: float = 50000,
                 check_interval: int = 3600,
                 mode: str = 'TESTING'):  # TESTING, SWING_TRADING, или INTRADAY_TRADING

        # Updated timeframes with proper periods
        self.timeframes = {
            'SWING_TRADING': {'trend': '1w', 'entry': '1d', 'trend_limit': 52},  # 1 year of weekly data
            'INTRADAY_TRADING': {'trend': '1d', 'entry': '1h', 'trend_limit': 180},  # 6 months of daily data
            'TESTING': {'trend': '1d', 'entry': '1h', 'trend_limit': 180}  # 6 months for testing
        }
        self.current_mode = mode

        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('crypto_monitor.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)  # Use stdout for console output
            ]
        )
        # Force console encoding to UTF-8
        if sys.platform.startswith('win'):
            import codecs
            sys.stdout.reconfigure(encoding='utf-8')
            sys.stderr.reconfigure(encoding='utf-8')

        self.logger = logging.getLogger(__name__)

        self.exchange = getattr(ccxt, exchange_id)()
        self.min_volume = min_volume
        self.check_interval = check_interval

    def calculate_indicators(self, df: pd.DataFrame) -> pd.DataFrame:
        """Расчет MACD и Elder's indicators"""
        try:
            # MACD(12,26,9)
            df['ema12'] = df['close'].ewm(span=12, adjust=False).mean()
            df['ema26'] = df['close'].ewm(span=26, adjust=False).mean()
            df['macd'] = df['ema12'] - df['ema26']
            df['macd_signal'] = df['macd'].ewm(span=9, adjust=False).mean()

            # Elder's favorites settings
            df['center_ema'] = df['close'].ewm(span=26, adjust=False).mean()
            df['short_ema'] = df['close'].ewm(span=12, adjust=False).mean()

            return df
        except Exception as e:
            self.logger.error(f'Ошибка при расчете индикаторов: {str(e)}')
            return df

    def analyze_trend(self, symbol: str) -> str:
        """Анализ тренда на старшем таймфрейме с использованием EMA и более длительного периода"""
        try:
            timeframe = self.timeframes[self.current_mode]['trend']
            limit = self.timeframes[self.current_mode]['trend_limit']
            self.logger.info(f"Анализируем тренд {symbol} на таймфрейме {timeframe} за период {limit}")

            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=limit)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            # Calculate Smart Vision EMA 20
            df['ema20'] = df['close'].ewm(span=20, adjust=False).mean()
            
            # Get last 20 periods for trend analysis
            last_20 = df.tail(20)
            
            # Calculate trend based on EMA and price action
            price_above_ema = last_20['close'].iloc[-1] > last_20['ema20'].iloc[-1]
            ema_slope = (last_20['ema20'].iloc[-1] - last_20['ema20'].iloc[0]) / len(last_20)
            
            # Check for higher highs and higher lows in the recent period
            recent_highs = last_20['high'].values
            recent_lows = last_20['low'].values
            
            # Calculate trends using local maxima and minima
            higher_highs = np.all(np.diff([h for h in recent_highs if h == np.maximum.accumulate(recent_highs)[recent_highs.tolist().index(h)]]) > 0)
            higher_lows = np.all(np.diff([l for l in recent_lows if l == np.minimum.accumulate(recent_lows)[recent_lows.tolist().index(l)]]) > 0)
            
            lower_highs = np.all(np.diff([h for h in recent_highs if h == np.maximum.accumulate(recent_highs)[::-1][recent_highs.tolist()[::-1].index(h)]]) < 0)
            lower_lows = np.all(np.diff([l for l in recent_lows if l == np.minimum.accumulate(recent_lows)[::-1][recent_lows.tolist()[::-1].index(l)]]) < 0)

            self.logger.info(f"""
            Trend Analysis Results:
            Price above EMA20: {price_above_ema}
            EMA Slope: {ema_slope}
            Higher Highs: {higher_highs}
            Higher Lows: {higher_lows}
            Lower Highs: {lower_highs}
            Lower Lows: {lower_lows}
            Last Price: {last_20['close'].iloc[-1]}
            """)

            # Determine trend with more flexible conditions
            if price_above_ema and ema_slope > 0:
                if higher_highs or higher_lows:
                    self.logger.info("Определен Strong BULLISH тренд")
                    return 'bullish'
                self.logger.info("Определен BULLISH тренд (based on EMA)")
                return 'bullish'
            elif not price_above_ema and ema_slope < 0:
                if lower_highs or lower_lows:
                    self.logger.info("Определен Strong BEARISH тренд")
                    return 'bearish'
                self.logger.info("Определен BEARISH тренд (based on EMA)")
                return 'bearish'

            self.logger.info("Определен NEUTRAL тренд")
            return 'neutral'

        except Exception as e:
            self.logger.error(f'Ошибка при анализе тренда: {str(e)}')
            self.logger.exception("Полный стек ошибки:")
            return 'neutral'

    def detect_shakeout(self, df: pd.DataFrame, trend: str) -> Dict:
        """
        Ищет точки входа в позицию на основе пробоя зоны ценности (EMA) после отката.
        
        Args:
            df: DataFrame со свечами (OHLCV данные)
            trend: Текущий тренд ('bullish' - восходящий, 'bearish' - нисходящий)
        
        Returns:
            Dict: Словарь с информацией о сигнале
        """
        try:
            # Рассчитываем технические индикаторы (MACD и EMA)
            df = self.calculate_indicators(df)
            
            # Берём последнюю завершенную свечку
            last_candle = df.iloc[-1]
            
            # Создаём структуру для хранения информации о сигнале
            signal = {
                'is_signal': False,            # Флаг наличия сигнала
                'type': None,                  # Тип сигнала (LONG или SHORT)
                'price': last_candle['close'], # Текущая цена закрытия
                'macd_value': last_candle['macd'],      # Значение MACD
                'ema_value': last_candle['center_ema']  # Значение EMA (зона ценности)
            }

            # Считаем положение цены относительно EMA
            price_to_ema = last_candle['close'] / last_candle['center_ema']

            # Логируем текущее состояние для отладки
            self.logger.info(f"""
            Анализ в реальном времени:
            - Текущая цена: {last_candle['close']:.2f}
            - EMA (зона ценности): {last_candle['center_ema']:.2f}
            - MACD: {last_candle['macd']:.6f}
            - Тренд: {trend}
            - Соотношение цена/EMA: {price_to_ema:.4f}
            """)

            # ЛОГИКА ДЛЯ БЫЧЬЕГО ТРЕНДА (LONG)
            if trend == 'bullish':
                # Проверяем условия для входа в LONG:
                # 1. Цена около или ниже EMA (откат завершился)
                # 2. MACD в красной зоне (подтверждение отката)
                if price_to_ema <= 1.02 and last_candle['macd'] < 0:
                    if self.is_new_signal(last_candle['close'], last_candle['center_ema'], last_candle['macd']):
                        self.logger.info(f"""
                        🎯 Найдена точка входа (LONG):
                        - Цена откатилась к зоне ценности
                        - Соотношение цена/EMA: {price_to_ema:.4f}
                        - MACD в красной зоне: {last_candle['macd']:.6f}
                        """)
                        signal['is_signal'] = True
                        signal['type'] = 'LONG'
                        self.update_last_signal(last_candle['close'], last_candle['center_ema'], 
                                             last_candle['macd'], 'LONG')

            # ЛОГИКА ДЛЯ МЕДВЕЖЬЕГО ТРЕНДА (SHORT)
            elif trend == 'bearish':
                # Проверяем условия для входа в SHORT:
                # 1. Цена около или выше EMA (откат завершился)
                # 2. MACD в зелёной зоне (подтверждение отката)
                if price_to_ema >= 0.98 and last_candle['macd'] > 0:
                    if self.is_new_signal(last_candle['close'], last_candle['center_ema'], last_candle['macd']):
                        self.logger.info(f"""
                        🎯 Найдена точка входа (SHORT):
                        - Цена поднялась к зоне ценности
                        - Соотношение цена/EMA: {price_to_ema:.4f}
                        - MACD в зелёной зоне: {last_candle['macd']:.6f}
                        """)
                        signal['is_signal'] = True
                        signal['type'] = 'SHORT'
                        self.update_last_signal(last_candle['close'], last_candle['center_ema'], 
                                             last_candle['macd'], 'SHORT')

            return signal

        except Exception as e:
            self.logger.error(f'Ошибка при определении встряски: {str(e)}')
            return {'is_signal': False, 'type': None, 'price': 0, 'macd_value': 0, 'ema_value': 0}

    async def run_forever(self, alert_callback=None):
        """Основной цикл мониторинга"""
        self.logger.info('Запуск мониторинга встрясок...')

        while True:
            try:
                # 1. Определяем тренд на старшем таймфрейме
                trend = self.analyze_trend('BTC/USDT')
                self.logger.info(f'Текущий тренд BTC: {trend}')

                if trend == 'neutral':
                    self.logger.info('Тренд нейтральный, пропускаем анализ')
                    time.sleep(self.check_interval)
                    continue

                # 2. Анализируем младший таймфрейм
                timeframe = self.timeframes[self.current_mode]['entry']
                ohlcv = self.exchange.fetch_ohlcv('BTC/USDT', timeframe, limit=100)
                df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])

                # 3. Ищем встряску
                signal = self.detect_shakeout(df, trend)

                if signal['is_signal']:
                    message = f"""
    🚨 Встряска на BTC!
    📈 Тренд: {trend}
    💰 Цена: {signal['price']:.2f}
    📊 Тип сигнала: {'🟢 LONG' if signal['type'] == 'LONG' else '🔴 SHORT'}
    ⏰ Время: {datetime.now()}

    🔍 MACD: {signal['macd_value']:.6f}
    """
                    if alert_callback:
                        await alert_callback(message)
                        self.logger.info(f'Отправлен сигнал о встряске: {signal["type"]}')

                time.sleep(self.check_interval)

            except Exception as e:
                self.logger.error(f'Ошибка в основном цикле: {str(e)}')
                time.sleep(60)


async def send_telegram_alert(message):
    async with aiohttp.ClientSession() as session:
        async with session.post("http://localhost:8000/send_message",
                                json={"text": message}) as response:
            await response.json()


async def main():
    monitor = CryptoShakeoutMonitor(
        exchange_id='binance',
        min_volume=50000,
        check_interval=120,
        mode='TESTING'  # Для быстрого тестирования
    )
    await monitor.run_forever(alert_callback=send_telegram_alert)


if __name__ == "__main__":
    asyncio.run(main())