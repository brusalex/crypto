import asyncio
import aiohttp
import ccxt
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import logging
from typing import List, Dict, Tuple
import time


class CryptoShakeoutMonitor:
    def __init__(self,
                 exchange_id: str = 'binance',
                 min_volume: float = 50000,
                 check_interval: int = 3600,
                 mode: str = 'TESTING'):  # TESTING, SWING_TRADING, или INTRADAY_TRADING

        # Настройка таймфреймов в зависимости от режима
        self.timeframes = {
            'SWING_TRADING': {'trend': '1w', 'entry': '1d'},
            'INTRADAY_TRADING': {'trend': '1d', 'entry': '1h'},
            'TESTING': {'trend': '1d', 'entry': '1h'}
        }
        self.current_mode = mode

        # Настройка логирования
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('crypto_monitor.log'),
                logging.StreamHandler()
            ]
        )
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
        """Анализ тренда на старшем таймфрейме"""
        try:
            timeframe = self.timeframes[self.current_mode]['trend']
            self.logger.info(f"Анализируем тренд {symbol} на таймфрейме {timeframe}")

            ohlcv = self.exchange.fetch_ohlcv(symbol, timeframe, limit=100)
            df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
            df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')

            self.logger.info(f"Получено {len(df)} свечей")

            # Берем последние 3 свечи
            last_3 = df.tail(3)

            # Проверяем тренд напрямую
            highs = last_3['high'].values
            lows = last_3['low'].values

            # Логируем значения
            self.logger.info(f"""
    Анализ последних 3 свечей:
    Максимумы (highs): {highs}
    Минимумы (lows): {lows}
    """)

            # Проверяем восходящий тренд
            higher_highs = (highs[1] > highs[0]) and (highs[2] > highs[1])
            higher_lows = (lows[1] > lows[0]) and (lows[2] > lows[1])

            # Проверяем нисходящий тренд
            lower_highs = (highs[1] < highs[0]) and (highs[2] < highs[1])
            lower_lows = (lows[1] < lows[0]) and (lows[2] < lows[1])

            self.logger.info(f"""
    Результаты анализа:
    Higher highs: {higher_highs}
    Higher lows: {higher_lows}
    Lower highs: {lower_highs}
    Lower lows: {lower_lows}
    """)

            if higher_highs and higher_lows:
                self.logger.info("Определен BULLISH тренд")
                return 'bullish'
            elif lower_highs and lower_lows:
                self.logger.info("Определен BEARISH тренд")
                return 'bearish'

            self.logger.info("Определен NEUTRAL тренд")
            return 'neutral'

        except Exception as e:
            self.logger.error(f'Ошибка при анализе тренда: {str(e)}')
            self.logger.exception("Полный стек ошибки:")
            return 'neutral'

    def detect_shakeout(self, df: pd.DataFrame, trend: str) -> Dict:
        """Определение паттерна встряски с типом сигнала"""
        try:
            df = self.calculate_indicators(df)
            last_row = df.iloc[-1]
            prev_row = df.iloc[-2]

            signal = {
                'is_signal': False,
                'type': None,  # 'LONG' или 'SHORT'
                'price': last_row['close'],
                'macd_value': last_row['macd']
            }

            if trend == 'bullish':
                # Сигнал на лонг в бычьем тренде
                price_near_ema = last_row['close'] <= last_row['center_ema']
                macd_improving = last_row['macd'] > prev_row['macd']

                if price_near_ema and macd_improving:
                    signal['is_signal'] = True
                    signal['type'] = 'LONG'

            elif trend == 'bearish':
                # Сигнал на шорт в медвежьем тренде
                price_near_ema = last_row['close'] >= last_row['center_ema']
                macd_declining = last_row['macd'] < prev_row['macd']

                if price_near_ema and macd_declining:
                    signal['is_signal'] = True
                    signal['type'] = 'SHORT'

            self.logger.info(f"Тренд: {trend}, Тип сигнала: {signal['type']}")
            return signal

        except Exception as e:
            self.logger.error(f'Ошибка при определении встряски: {str(e)}')
            return {'is_signal': False, 'type': None}

    # def detect_shakeout2(self, df: pd.DataFrame, trend: str) -> bool:
    #     """Определение паттерна встряски"""
    #     try:
    #         df = self.calculate_indicators(df)
    #         last_row = df.iloc[-1]
    #         prev_row = df.iloc[-2]
    #         self.logger.info(f"""
    #         Checking shakeout:
    #         - Price: {last_row['close']}
    #         - EMA: {last_row['center_ema']}
    #         - MACD current: {last_row['macd']}
    #         - MACD previous: {prev_row['macd']}
    #         """)
    #
    #         if trend == 'bullish':
    #             # Цена у средней или ниже + красная линия MACD начинает белеть
    #             price_near_ema = last_row['close'] <= last_row['center_ema']
    #             macd_improving = last_row['macd'] > prev_row['macd']
    #             self.logger.info(f"""
    #             Bullish conditions:
    #             - Price near EMA: {price_near_ema}
    #             - MACD improving: {macd_improving}
    #             """)
    #             return price_near_ema and macd_improving
    #
    #         elif trend == 'bearish':
    #             # Цена у средней или выше + зеленая линия MACD начинает белеть
    #             price_near_ema = last_row['close'] >= last_row['center_ema']
    #             macd_declining = last_row['macd'] < prev_row['macd']
    #             return price_near_ema and macd_declining
    #
    #         return False
    #
    #     except Exception as e:
    #         self.logger.error(f'Ошибка при определении встряски: {str(e)}')
    #         return False

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