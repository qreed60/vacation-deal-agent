from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from typing import Any
from urllib.parse import urlparse

from sqlmodel import Session, select

from app.db.models import DealCandidate, PriceSnapshot


def is_google_flights_url(url: str | None) -> bool:
    """Check if a URL is a Google Flights search URL."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = parsed.netloc.lower()
        # Google Flights domains and paths
        if "google.com" in host or "google travel" in host.lower():
            if "/travel/flights" in parsed.path.lower() or "flights" in parsed.query.lower():
                return True
        # Also check for google.com/travel pattern
        if host.startswith("www.google.") and "/travel" in parsed.path:
            return True
    except Exception:
        pass
    return False


def get_source_link_label(source_url: str | None, source_name: str | None = None) -> tuple[str, str]:
    """
    Get the appropriate label for a source link.
    Returns (link_type, label) tuple.
    """
    if not source_url:
        return ("none", None)
    
    if is_google_flights_url(source_url):
        return ("search_reference", "Google Flights search")
    
    # Check if it's a provider domain (not a search aggregator)
    try:
        parsed = urlparse(source_url)
        host = parsed.netloc.lower()
        # Common provider/airline patterns - not search aggregators
        provider_patterns = [
            "united.com", "delta.com", "american.com", "southwest.com",
            "jetblue.com", "alaska.com", "frontier.com", "spirit.com",
            "expedia.com", "booking.com", "hotels.com", "priceline.com",
            "kayak.com", "orbitz.com", "travelocity.com",
            "enterprise.com", "hertz.com", "avis.com", "budget.com",
        ]
        for pattern in provider_patterns:
            if pattern in host:
                return ("exact_source", "Provider page")
    except Exception:
        pass
    
    # Default to exact source if we have a URL
    return ("exact_source", "View source price")


def aggregate_daily_ohlc(
    history_rows: list[dict[str, Any]],
    component_type: str | None = None,
    exclude_mock: bool = True,
) -> list[dict[str, Any]]:
    """
    Aggregate price history by calendar day into OHLC-style data.
    
    Args:
        history_rows: List of history rows with timestamp, total_price, currency, etc.
        component_type: Filter by component type (e.g., 'flight', 'hotel', 'rental_car')
        exclude_mock: If True, exclude mock rows from aggregation
        
    Returns:
        List of daily OHLC records with date, open, high, low, close, currency, count, component_type
    """
    # Filter rows
    filtered_rows = []
    for row in history_rows:
        # Skip mock rows if requested
        if exclude_mock and row.get("is_mock", False):
            continue
        # Skip rows without price
        if row.get("total_price") is None:
            continue
        # Filter by component type if specified
        if component_type and row.get("quote_type") != component_type:
            continue
        filtered_rows.append(row)
    
    if not filtered_rows:
        return []
    
    # Group by date and currency
    # Structure: {(date_str, currency): [rows]}
    daily_groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in filtered_rows:
        ts = row.get("timestamp")
        if isinstance(ts, datetime):
            date_str = ts.date().isoformat()
        elif isinstance(ts, date):
            date_str = ts.isoformat()
        else:
            # Try to parse string timestamp
            try:
                if "T" in str(ts):
                    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                else:
                    dt = datetime.strptime(str(ts)[:10], "%Y-%m-%d")
                date_str = dt.date().isoformat()
            except (ValueError, TypeError):
                continue
        
        currency = row.get("currency", "USD")
        daily_groups[(date_str, currency)].append(row)
    
    # Sort dates
    sorted_dates = sorted(daily_groups.keys())
    
    # Build OHLC data
    ohlc_data = []
    previous_close: dict[tuple[str, str], float] = {}  # {(date, currency): close_price}
    
    for date_key in sorted_dates:
        date_str, currency = date_key
        rows = daily_groups[date_key]
        
        # Sort rows by timestamp within the day
        def get_ts(r):
            ts = r.get("timestamp")
            if isinstance(ts, datetime):
                return ts
            try:
                if "T" in str(ts):
                    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                return datetime.strptime(str(ts)[:10], "%Y-%m-%d")
            except (ValueError, TypeError):
                return datetime.min
        
        rows_sorted = sorted(rows, key=get_ts)
        prices = [float(r["total_price"]) for r in rows_sorted]
        
        # Calculate OHLC
        open_price = previous_close.get(date_key)
        if open_price is None:
            open_price = prices[0]  # First price of the day if no previous close
        
        close_price = prices[-1]  # Last price of the day
        high_price = max(prices)
        low_price = min(prices)
        
        # Store this day's close for next day's open
        previous_close[date_key] = close_price
        
        # Get component type from first row
        comp_type = rows_sorted[0].get("quote_type", "unknown")
        
        ohlc_data.append({
            "date": date_str,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "currency": currency,
            "count": len(rows),
            "component_type": comp_type,
        })
    
    return ohlc_data


def price_snapshot_history(
    session: Session,
    vacation_id: int,
    include_mock: bool = False,
) -> list[dict[str, Any]]:
    query = select(PriceSnapshot).where(
        PriceSnapshot.vacation_id == vacation_id,
        PriceSnapshot.total_price.is_not(None),
    ).order_by(PriceSnapshot.captured_at.asc(), PriceSnapshot.id.asc())
    
    if not include_mock:
        query = query.where(PriceSnapshot.is_mock == False)
    
    snapshots = session.exec(query).all()
    return [
        {
            "timestamp": snapshot.captured_at,
            "quote_type": snapshot.quote_type,
            "total_price": snapshot.total_price,
            "currency": snapshot.currency,
            "source_name": snapshot.source_name,
            "label": snapshot.label,
            "is_mock": snapshot.is_mock,
            "source_url": snapshot.source_url,
        }
        for snapshot in snapshots
    ]


def deal_candidate_history(
    session: Session,
    vacation_id: int,
    include_mock: bool = False,
) -> list[dict[str, Any]]:
    query = select(DealCandidate).where(
        DealCandidate.vacation_id == vacation_id,
        DealCandidate.total_price.is_not(None),
    ).order_by(DealCandidate.created_at.asc(), DealCandidate.id.asc())
    
    if not include_mock:
        query = query.where(DealCandidate.is_mock == False)
    
    candidates = session.exec(query).all()
    return [
        {
            "timestamp": candidate.created_at,
            "quote_type": candidate.candidate_type,
            "total_price": candidate.total_price,
            "currency": candidate.currency,
            "source_name": "deal_candidate",
            "label": candidate.title,
            "is_mock": candidate.is_mock,
        }
        for candidate in candidates
    ]


def vacation_price_history(
    session: Session,
    vacation_id: int,
    include_mock: bool = False,
    component_type: str | None = None,
) -> dict[str, Any]:
    """Get vacation price history with optional filtering and OHLC aggregation."""
    snapshots = price_snapshot_history(session, vacation_id, include_mock=include_mock)
    deals = deal_candidate_history(session, vacation_id, include_mock=include_mock)
    
    # Combine for raw history
    all_raw = snapshots + deals
    
    # Generate OHLC aggregated data
    ohlc_data = aggregate_daily_ohlc(all_raw, component_type=component_type, exclude_mock=not include_mock)
    
    return {
        "snapshots": snapshots,
        "deals": deals,
        "ohlc": ohlc_data,
        "raw_all": all_raw,
    }


def svg_line_points(history_rows: list[dict[str, Any]], *, width: int = 640, height: int = 180) -> str:
    priced_rows = [row for row in history_rows if row.get("total_price") is not None]
    if not priced_rows:
        return ""
    prices = [float(row["total_price"]) for row in priced_rows]
    min_price = min(prices)
    max_price = max(prices)
    left = 24
    right = width - 16
    top = 16
    bottom = height - 24
    x_span = max(1, right - left)
    y_span = max(1, bottom - top)
    price_span = max(1.0, max_price - min_price)
    points = []
    for index, price in enumerate(prices):
        x = left + (x_span * index / max(1, len(prices) - 1))
        y = bottom - ((price - min_price) / price_span * y_span)
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


def svg_ohlc_candles(
    ohlc_data: list[dict[str, Any]],
    *,
    width: int = 640,
    height: int = 180,
) -> str:
    """
    Generate SVG for OHLC candlestick/bar chart.
    Uses vertical bars with open/close ticks.
    """
    if not ohlc_data:
        return ""
    
    left = 40
    right = width - 16
    top = 16
    bottom = height - 36  # Leave room for X-axis labels
    
    # Get price range
    all_prices = []
    for d in ohlc_data:
        all_prices.extend([d["low"], d["high"]])
    
    if not all_prices:
        return ""
    
    min_price = min(all_prices)
    max_price = max(all_prices)
    price_span = max(1.0, max_price - min_price)
    
    x_span = right - left
    y_span = bottom - top
    
    n = len(ohlc_data)
    if n == 0:
        return ""
    
    # Calculate bar width and spacing
    bar_width = max(4, min(20, x_span / n * 0.7))
    spacing = x_span / n
    center_offset = spacing / 2
    
    svg_parts = []
    
    # Draw grid lines
    grid_count = 5
    for i in range(grid_count + 1):
        y = top + (y_span * i / grid_count)
        price_val = max_price - (price_span * i / grid_count)
        svg_parts.append(
            f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="#e0e0e0" stroke-width="1"/>'
        )
        # Price label
        svg_parts.append(
            f'<text x="{left - 4}" y="{y + 4}" text-anchor="end" font-size="10" fill="#666">${price_val:.0f}</text>'
        )
    
    # Draw OHLC bars
    for i, d in enumerate(ohlc_data):
        x_center = left + (i * spacing) + center_offset
        x_left = x_center - bar_width / 2
        x_right = x_center + bar_width / 2
        
        # Calculate Y positions
        y_high = bottom - ((d["high"] - min_price) / price_span * y_span)
        y_low = bottom - ((d["low"] - min_price) / price_span * y_span)
        y_open = bottom - ((d["open"] - min_price) / price_span * y_span)
        y_close = bottom - ((d["close"] - min_price) / price_span * y_span)
        
        # Determine color (green if close >= open, red otherwise)
        color = "#22c55e" if d["close"] >= d["open"] else "#ef4444"
        
        # Draw vertical line (high to low)
        svg_parts.append(
            f'<line x1="{x_center}" y1="{y_high}" x2="{x_center}" y2="{y_low}" stroke="{color}" stroke-width="2"/>'
        )
        
        # Draw open tick
        svg_parts.append(
            f'<line x1="{x_left}" y1="{y_open}" x2="{x_center}" y2="{y_open}" stroke="{color}" stroke-width="2"/>'
        )
        
        # Draw close tick
        svg_parts.append(
            f'<line x1="{x_center}" y1="{y_close}" x2="{x_right}" y2="{y_close}" stroke="{color}" stroke-width="2"/>'
        )
        
        # Date label on X axis
        date_label = d["date"][5:]  # MM-DD format
        svg_parts.append(
            f'<text x="{x_center}" y="{bottom + 14}" text-anchor="middle" font-size="9" fill="#666">{date_label}</text>'
        )
    
    # Axis labels
    svg_parts.append(
        f'<text x="{width / 2}" y="{height - 2}" text-anchor="middle" font-size="11" font-weight="600" fill="#333">Date</text>'
    )
    svg_parts.append(
        f'<text x="12" y="{height / 2}" text-anchor="middle" font-size="11" font-weight="600" fill="#333" transform="rotate(-90, 12, {height / 2})">Price</text>'
    )
    
    return "\n".join(svg_parts)
