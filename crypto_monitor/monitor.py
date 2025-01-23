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

        # Состояния для отслеживания движения цены
        self.price_states = {}  # Словарь для хранения состояний цен по парам
        
        # Параметры для определения параболического движения
        self.price_history_length = 10  # Сколько точек хранить для анализа движения
        self.min_price_move = 0.005     # Минимальное движение цены (0.5%)

        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('crypto_monitor.log', encoding='utf-8'),
                logging.StreamHandler(sys.stdout)
            ]
        )
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

    def update_price_state(self, symbol: str, price_to_ema: float, is_above_ema: bool, price: float) -> None:
        """
        Обновляет состояние цены и отслеживает параболическое движение.
        """
        if symbol not in self.price_states:
            self.price_states[symbol] = {
                'was_above_ema': False,      # Была ли цена выше EMA
                'highest_price_to_ema': 0.0,  # Максимальное отношение цены к EMA
                'movement_started': False,    # Начался ли откат
                'price_history': [],         # История движения цены
                'ema_history': [],           # История значений EMA
                'movement_type': None        # Тип движения (None, 'up', 'down', 'parabolic')
            }
        
        state = self.price_states[symbol]
        
        # Обновляем историю цен и EMA
        state['price_history'].append(price)
        if len(state['price_history']) > self.price_history_length:
            state['price_history'].pop(0)
        
        # Если цена выше EMA
        if is_above_ema:
            state['was_above_ema'] = True
            if price_to_ema > state['highest_price_to_ema']:
                state['highest_price_to_ema'] = price_to_ema
                state['movement_started'] = False
                state['movement_type'] = 'up'
        
        # Определяем тип движения на основе истории цен
        if len(state['price_history']) >= 3:
            prices = state['price_history']
            
            # Проверяем изменение направления движения
            last_moves = [
                (prices[-1] - prices[-2]) / prices[-2],  # Последнее движение
                (prices[-2] - prices[-3]) / prices[-3]   # Предпоследнее движение
            ]
            
            # Если было движение вверх, а теперь вниз
            if (state['movement_type'] == 'up' and 
                last_moves[0] < -self.min_price_move):
                state['movement_type'] = 'down'
                state['movement_started'] = True
            
            # Если движение параболическое (разворот)
            if (state['was_above_ema'] and 
                state['movement_type'] == 'down' and 
                last_moves[0] > self.min_price_move and  # Цена начала расти
                price_to_ema < 1.0):  # Но всё ещё ниже EMA
                state['movement_type'] = 'parabolic'

    def detect_shakeout(self, df: pd.DataFrame, trend: str) -> Dict:
        """Определение сигнала встряски на основе следующих условий:
        - На дневке бычий тренд
        - На часовике цена была выше зоны ценности
        - Откатилась к ней и опустилась ниже
        - MACD находится в красной зоне
        """
        try:
            df = self.calculate_indicators(df)

            # Берем последние 24 свечи (~сутки)
            recent_candles = df.tail(24).copy()
            last_candle = recent_candles.iloc[-1]

            signal = {
                'is_signal': False,
                'type': None,
                'price': last_candle['close'],
                'macd_value': last_candle['macd'],
                'ema_value': last_candle['center_ema']
            }

            # Логируем текущее состояние для отладки
            self.logger.info(f"""
                Анализ часового графика:
                - Цена: {last_candle['close']:.2f}
                - EMA (зона ценности): {last_candle['center_ema']:.2f}
                - MACD: {last_candle['macd']:.6f}
                - Тренд: {trend}
                """)

            if trend == 'bullish':
                # 1. Проверяем, была ли цена выше EMA за последние 24 часа
                was_above_ema = any(candle['close'] > candle['center_ema']
                                    for _, candle in recent_candles.iterrows())

                # 2. Проверяем текущее положение цены относительно EMA
                current_below_ema = last_candle['close'] < last_candle['center_ema']

                # 3. Проверяем MACD в красной зоне
                macd_below_zero = last_candle['macd'] < 0

                # Все условия для сигнала
                if was_above_ema and current_below_ema and macd_below_zero:
                    self.logger.info(f"""
                        🎯 Найдена точка входа (LONG):
                        - Цена около EMA: {current_below_ema}
                        - MACD в красной зоне: {macd_below_zero}
                        """)
                    signal['is_signal'] = True
                    signal['type'] = 'LONG'

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
                signal = self.detect_shakeout(df, trend, 'BTC/USDT')

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