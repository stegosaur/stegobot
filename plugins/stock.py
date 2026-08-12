"""Stock quote plugin — powered by yfinance 1.5.2.

Usage (channel):  <botnick>: stock AAPL
Usage (PM):       stock AAPL   or   quote AAPL

PUBLIC = True so anyone can use it.
Output is 3 IRC lines per lookup.
"""

from datetime import datetime, timezone
import time
import yfinance as yf

COMMANDS = ['stock', 'quote']
PUBLIC   = True


# ── Formatting helpers ──────────────────────────────────────────────────────

def _p(val, decimals=2):
    """Format a price/number with $ and commas, or N/A."""
    if val is None:
        return 'N/A'
    return f'${val:,.{decimals}f}'


def _cap(val):
    """Format a market-cap number as $X.XXB / $X.XXT etc."""
    if val is None:
        return 'N/A'
    if val >= 1e12:
        return f'${val/1e12:.2f}T'
    if val >= 1e9:
        return f'${val/1e9:.2f}B'
    if val >= 1e6:
        return f'${val/1e6:.2f}M'
    return f'${val:,.0f}'


def _date(val):
    """Convert a Unix timestamp or datetime.date to 'Mon DD, YYYY' string."""
    if val is None:
        return 'N/A'
    try:
        if isinstance(val, (int, float)):
            return datetime.fromtimestamp(val, tz=timezone.utc).strftime('%b %d, %Y')
        if hasattr(val, 'strftime'):
            return val.strftime('%b %d, %Y')
        return str(val)
    except Exception:
        return 'N/A'


def handle(cmd, args, ctx):
    symbol = args.strip().upper()
    if not symbol:
        ctx['reply']('Usage: stock <ticker>  e.g. stock AAPL')
        return True

    try:
        tk   = yf.Ticker(symbol)
        info = tk.info
    except Exception as exc:
        ctx['reply'](f'Error fetching {symbol}: {exc}')
        return True

    # Validate — if there's no price the ticker is bogus
    price = info.get('regularMarketPrice') or info.get('currentPrice')
    if price is None:
        try:
            hist = tk.history(period='1d')
            if not hist.empty:
                price = float(hist['Close'].iloc[-1])
        except Exception:
            pass
    if price is None:
        ctx['reply'](f'No data found for "{symbol}". Check the ticker symbol.')
        return True

    name     = info.get('longName') or info.get('shortName') or symbol
    prev     = info.get('previousClose') or info.get('regularMarketPreviousClose')
    open_p   = info.get('open') or info.get('regularMarketOpen')
    bid      = info.get('bid')
    bid_sz   = info.get('bidSize')
    ask      = info.get('ask')
    ask_sz   = info.get('askSize')
    day_lo   = info.get('dayLow')  or info.get('regularMarketDayLow')
    day_hi   = info.get('dayHigh') or info.get('regularMarketDayHigh')
    wk52_lo  = info.get('fiftyTwoWeekLow')
    wk52_hi  = info.get('fiftyTwoWeekHigh')
    mkt_cap  = info.get('marketCap')
    pe       = info.get('trailingPE')
    eps      = info.get('trailingEps') or info.get('epsTrailingTwelveMonths')
    div_rate = info.get('dividendRate')
    div_yld  = info.get('dividendYield')   # yfinance 1.5.x returns this as a % (e.g. 0.35 = 0.35%)
    ex_div   = info.get('exDividendDate')  # Unix timestamp

    # ── Price change ───────────────────────────────────────────────────────
    if prev:
        chg     = price - prev
        chg_pct = (chg / prev) * 100
        arrow   = '▲' if chg >= 0 else '▼'
        sign    = '+' if chg >= 0 else ''
        chg_str = f'{arrow} {sign}${abs(chg):.2f} ({sign}{chg_pct:.2f}%)'
    else:
        chg_str = ''

    # ── Earnings date (next, from calendar) ───────────────────────────────
    earn_str = 'N/A'
    try:
        cal = tk.calendar
        if isinstance(cal, dict):
            dates = cal.get('Earnings Date')
            if dates:
                earn_str = _date(dates[0] if isinstance(dates, (list, tuple)) else dates)
        elif cal is not None and hasattr(cal, 'iloc'):
            earn_str = _date(cal.iloc[0, 0])
    except Exception:
        pass

    # ── Dividend ───────────────────────────────────────────────────────────
    if div_rate and div_yld is not None:
        div_str = f'${div_rate:.2f}/yr ({div_yld:.2f}%)'
    elif div_rate:
        div_str = f'${div_rate:.2f}/yr'
    else:
        div_str = 'N/A'

    # ── Bid/Ask ────────────────────────────────────────────────────────────
    if bid and ask:
        ba = f'{_p(bid)}×{bid_sz or "?"} / {_p(ask)}×{ask_sz or "?"}'
    elif bid:
        ba = _p(bid)
    elif ask:
        ba = _p(ask)
    else:
        ba = 'N/A'

    # ── Ranges ────────────────────────────────────────────────────────────
    day_range  = f'{_p(day_lo)} – {_p(day_hi)}' if day_lo and day_hi else 'N/A'
    wk52_range = f'{_p(wk52_lo)} – {_p(wk52_hi)}' if wk52_lo and wk52_hi else 'N/A'

    pe_str  = f'{pe:.2f}' if pe  else 'N/A'
    eps_str = _p(eps)  if eps else 'N/A'

    B = '\x02'   # IRC bold
    ctx['reply'](
        f'{B}[{symbol}]{B} {name} — {_p(price)} {chg_str}'
    )
    time.sleep(0.5)
    ctx['reply'](
        f'Prev Close: {_p(prev)} | Open: {_p(open_p)} | '
        f'Bid/Ask: {ba} | '
        f'Range: {day_range} | 52Wk: {wk52_range}'
    )
    time.sleep(0.5)
    ctx['reply'](
        f'Cap: {_cap(mkt_cap)} | P/E: {pe_str} | EPS: {eps_str} | '
        f'Earnings: {earn_str} | '
        f'Div: {div_str} | Ex-Div: {_date(ex_div)}'
    )
    return True
