"""
PricingEngineService:
Resolves effective selling price for products using a strict 3-tier hierarchy:
  1. Tier 1 (Highest Priority): Customer-specific active price list.
  2. Tier 2 (Secondary Priority): Sector-specific active price list (if customer belongs to a sector).
  3. Tier 3 (Base Default): Default product card sale price (or branch-specific sale price).

Units of measure:
  - PriceListItem.uom is an inventory.UnitOfMeasure. An item with uom=NULL is a base-unit price.
  - Without a uom only base-unit items (uom IS NULL) are considered.
  - With a uom, a uom-specific item wins; otherwise the base-unit item is used and scaled by the
    product's ProductUoM.conversion_factor. Percentage discounts of uom-specific items are applied
    to (base price x conversion factor).
"""
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q
from apps.tenant.pricing.models import PriceList, PriceListItem


def _active_lists(qs, as_of_date):
    return qs.filter(is_active=True).filter(
        Q(start_date__isnull=True) | Q(start_date__lte=as_of_date),
        Q(end_date__isnull=True) | Q(end_date__gte=as_of_date)
    )


def _normalize_uom(product, uom):
    """
    Accepts a UnitOfMeasure, a ProductUoM, or None.
    Returns (UnitOfMeasure|None, conversion factor Decimal).
    """
    if uom is None:
        return None, Decimal('1')
    # ProductUoM instance (has .uom and .conversion_factor)
    if hasattr(uom, 'conversion_factor') and hasattr(uom, 'uom_id'):
        factor = Decimal(str(uom.conversion_factor or 1))
        return uom.uom, (factor if factor > 0 else Decimal('1'))
    factor = Decimal('1')
    try:
        from apps.tenant.inventory.models import ProductUoM
        puom = ProductUoM.objects.filter(product=product, uom=uom).only('conversion_factor').first()
        if puom and puom.conversion_factor and puom.conversion_factor > 0:
            factor = Decimal(str(puom.conversion_factor))
    except Exception:
        pass
    return uom, factor


# highest quantity break first; on a tie the newest price list wins (pos.js keeps this order: stable sort)
_ITEM_ORDER = ('-min_quantity', '-price_list__created_at', '-price_list_id')


def _match_item(item_qs, uom):
    """Pick the best item (highest min_quantity) for the requested unit. Returns (item, is_uom_specific)."""
    if uom is not None:
        uom_item = item_qs.filter(uom=uom).order_by(*_ITEM_ORDER).first()
        if uom_item:
            return uom_item, True
    return item_qs.filter(uom__isnull=True).order_by(*_ITEM_ORDER).first(), False


class PricingEngineService:
    @classmethod
    def get_effective_price(
        cls,
        product,
        partner=None,
        branch=None,
        uom=None,
        quantity=Decimal('1'),
        as_of_date=None,
        price_per_uom=False,
    ) -> dict:
        """
        Calculates and returns the effective price for a product.

        Returns a dictionary with:
          - price: Decimal final unit price (see note below)
          - unit_price: Decimal price of ONE requested uom (base unit when uom is None)
          - base_unit_price: Decimal unit_price / uom_factor
          - uom_factor: Decimal conversion factor of the requested uom (1 for base unit)
          - price_is_per_uom: bool, whether `price` is per requested uom (True) or per base unit
          - tier: 'CUSTOMER' | 'SECTOR' | 'DEFAULT'
          - tier_display: Human-readable Arabic label of the price source
          - price_list_name: Name of the active price list applied (or None)
          - original_price: Decimal base price of the product (same unit as `price`)
          - discount_amount: Decimal savings per unit compared to default price (same unit as `price`)
          - discount_percentage: Decimal percentage saved
          - badge_color: Color hint for UI display

        Note on `price` (backward compatibility): with price_per_uom=False (default) `price`
        keeps its historical meaning: per base unit, except for a uom-specific FIXED item where it
        is the custom price of that uom. New callers should pass price_per_uom=True or use
        `unit_price`, which is always the price of one requested uom.
        """
        if as_of_date is None:
            as_of_date = timezone.localdate()

        try:
            quantity = Decimal(str(quantity or 1))
        except Exception:
            quantity = Decimal('1')
        if not quantity.is_finite() or quantity <= Decimal('0'):
            quantity = Decimal('1')

        uom, factor = _normalize_uom(product, uom)

        # Determine base product price (branch price if available, else product.sale_price)
        if branch and hasattr(product, 'get_price_for_branch'):
            base_price = Decimal(str(product.get_price_for_branch(branch) or 0))
        else:
            base_price = Decimal(str(getattr(product, 'sale_price', Decimal('0')) or 0))

        unit_original = base_price * factor

        def _result(tier, tier_display, item, unit_price, uom_specific, badge_color, badge_text, badge_class):
            unit_price = Decimal(str(unit_price))
            if price_per_uom:
                price, original, is_per_uom = unit_price, unit_original, True
            elif uom_specific and item is not None and item.pricing_method == PriceListItem.FIXED:
                # historical behaviour: custom price of the specific uom returned as-is
                price, original, is_per_uom = unit_price, unit_original, True
            else:
                price = (unit_price / factor) if factor != 1 else unit_price
                price = price.quantize(Decimal('0.0001'))
                original, is_per_uom = base_price, False
            discount_amount = max(Decimal('0'), original - price)
            discount_pct = ((discount_amount / original) * Decimal('100')).quantize(Decimal('0.01')) if original > 0 else Decimal('0')
            return {
                'price': price,
                'unit_price': unit_price.quantize(Decimal('0.0001')),
                'base_unit_price': (unit_price / factor).quantize(Decimal('0.0001')) if factor else unit_price,
                'uom_factor': factor,
                'price_is_per_uom': is_per_uom,
                'tier': tier,
                'tier_display': tier_display,
                'price_list_name': item.price_list.name if item is not None else None,
                'price_list_id': item.price_list.id if item is not None else None,
                'original_price': original,
                'discount_amount': discount_amount,
                'discount_percentage': discount_pct,
                'badge_color': badge_color,
                'badge_text': badge_text,
                'badge_class': badge_class,
            }

        def _unit_price_for(item, uom_specific):
            if uom_specific:
                return item.calculate_price(unit_original)
            return item.calculate_price(base_price) * factor

        # ---------------------------------------------------------------------
        # TIER 1: Customer-Specific Price List (أولوية قصوى)
        # ---------------------------------------------------------------------
        if partner and getattr(partner, 'partner_type', None) in ['CUSTOMER', 'BOTH']:
            customer_lists_qs = _active_lists(
                PriceList.objects.filter(list_type=PriceList.CUSTOMER, partner=partner), as_of_date
            )
            if customer_lists_qs.exists():
                item_qs = PriceListItem.objects.filter(
                    price_list__in=customer_lists_qs,
                    product=product,
                    min_quantity__lte=quantity
                ).select_related('price_list')
                matched_item, uom_specific = _match_item(item_qs, uom)
                if matched_item:
                    return _result(
                        'CUSTOMER', f'سعر خاص للعميل ({partner.name})', matched_item,
                        _unit_price_for(matched_item, uom_specific), uom_specific,
                        '#ff7300', 'سعر عميل خاص ⭐', 'badge-orange',
                    )

        # ---------------------------------------------------------------------
        # TIER 2: Sector-Specific Price List (أولوية القطاع)
        # ---------------------------------------------------------------------
        partner_sector = getattr(partner, 'sector', None) if partner else None
        if partner_sector and getattr(partner_sector, 'is_active', True):
            sector_lists_qs = _active_lists(
                PriceList.objects.filter(list_type=PriceList.SECTOR, sector=partner_sector), as_of_date
            )
            if sector_lists_qs.exists():
                item_qs = PriceListItem.objects.filter(
                    price_list__in=sector_lists_qs,
                    product=product,
                    min_quantity__lte=quantity
                ).select_related('price_list')
                matched_item, uom_specific = _match_item(item_qs, uom)
                if matched_item:
                    return _result(
                        'SECTOR', f'سعر قطاع: {partner_sector.name}', matched_item,
                        _unit_price_for(matched_item, uom_specific), uom_specific,
                        '#00e5ff', f'قطاع {partner_sector.name} 🏢', 'badge-info',
                    )

        # ---------------------------------------------------------------------
        # TIER 3: Default Product Card Price (السعر الافتراضي للصنف)
        # ---------------------------------------------------------------------
        res = _result(
            'DEFAULT', 'سعر الصنف الافتراضي', None, unit_original, False,
            '#94a3b8', 'سعر افتراضي 📦', 'badge-secondary',
        )
        res['discount_amount'] = Decimal('0.00')
        res['discount_percentage'] = Decimal('0.00')
        return res

    @classmethod
    def get_customer_price_tiers(cls, partner, branch=None, as_of_date=None) -> dict:
        """
        Returns product_id (str) -> list of tiers, each:
            {
              'tier': 'CUSTOMER' | 'SECTOR',
              'uom_id': UnitOfMeasure id or None (None = base unit),
              'min_quantity': float,
              'price': float   # price of ONE unit of uom_id (base unit when None)
              'base_price': float  # product base price of one unit of uom_id
              'pricing_method': 'FIXED' | 'DISCOUNT',
              'discount_percentage': float | None,
              'price_list_name': str,
            }
        Selection rule (same as get_effective_price): for a cart line with quantity q (in that uom)
        and unit u: first try CUSTOMER tiers then SECTOR tiers; within a tier prefer uom_id == u,
        else uom_id None (scaled by the unit factor); pick the highest min_quantity <= q.
        """
        if as_of_date is None:
            as_of_date = timezone.localdate()
        if not partner:
            return {}

        list_sources = []
        partner_sector = getattr(partner, 'sector', None)
        if partner_sector and getattr(partner_sector, 'is_active', True):
            list_sources.append(('SECTOR', _active_lists(
                PriceList.objects.filter(list_type=PriceList.SECTOR, sector=partner_sector), as_of_date)))
        if getattr(partner, 'partner_type', None) in ['CUSTOMER', 'BOTH']:
            list_sources.append(('CUSTOMER', _active_lists(
                PriceList.objects.filter(list_type=PriceList.CUSTOMER, partner=partner), as_of_date)))

        from apps.tenant.inventory.models import ProductUoM

        from apps.tenant.inventory.models import Product

        # All items first, then branch prices and unit factors for their products in
        # 2 queries (not one query per product / per unit).
        sources = [(tier_name, list(PriceListItem.objects.filter(price_list__in=lists_qs)
                                    .select_related('product', 'price_list')
                                    .order_by('product_id', 'min_quantity', '-price_list__created_at', '-price_list_id')))
                   for tier_name, lists_qs in list_sources]
        product_ids = {item.product_id for _, items in sources for item in items}
        branch_prices = Product.branch_price_map(product_ids, branch) if branch else {}
        factor_cache = {}
        for pid, uom_id, f in ProductUoM.objects.filter(product_id__in=product_ids).values_list(
                'product_id', 'uom_id', 'conversion_factor'):
            factor_cache[(pid, uom_id)] = Decimal(str(f)) if f and f > 0 else Decimal('1')

        tiers = {}
        for tier_name, items in sources:
            for item in items:
                pid = item.product_id
                base = Decimal(str(branch_prices.get(pid, item.product.sale_price) or 0))
                factor = factor_cache.get((pid, item.uom_id), Decimal('1')) if item.uom_id else Decimal('1')
                unit_base = base * factor
                tiers.setdefault(str(pid), []).append({
                    'tier': tier_name,
                    'uom_id': item.uom_id,
                    'min_quantity': float(item.min_quantity or 0),
                    'price': float(item.calculate_price(unit_base)),
                    'base_price': float(unit_base),
                    'pricing_method': item.pricing_method,
                    'discount_percentage': float(item.discount_percentage) if item.discount_percentage is not None else None,
                    'price_list_name': item.price_list.name,
                })
        return tiers

    @classmethod
    def get_customer_price_map(cls, partner, branch=None, as_of_date=None, include_tiers=True, tiers=None) -> dict:
        """
        Returns a dictionary mapping product_id (str) -> {
            'price': float,            # price for quantity 1 of the BASE unit (legacy meaning)
            'original_price': float,
            'tier': 'CUSTOMER' | 'SECTOR',
            'badge_text': str,
            'badge_color': str,
            'price_list_name': str,
            'tiers': [...],            # all tiers of the product (see get_customer_price_tiers)
        }
        Only products that have a base-unit (uom NULL) item applicable at quantity 1 get an entry
        in this map; quantity-break and unit-specific prices are exposed via 'tiers' (and via
        get_customer_price_tiers for products without a qty-1 base price).
        Tier 1 (Customer-specific) items override Tier 2 (Sector-specific) items.
        """
        if tiers is None:   # callers that already have the tiers pass them (no second computation)
            tiers = cls.get_customer_price_tiers(partner, branch=branch, as_of_date=as_of_date)
        if not tiers:
            return {}

        sector_name = getattr(getattr(partner, 'sector', None), 'name', '')
        prices_map = {}
        for pid, product_tiers in tiers.items():
            best = None
            for t in product_tiers:
                if t['uom_id'] is not None or t['min_quantity'] > 1:
                    continue
                if best is None:
                    best = t
                    continue
                # CUSTOMER beats SECTOR; within a tier the highest min_quantity (<= 1) wins
                rank = (t['tier'] == 'CUSTOMER', t['min_quantity'])
                best_rank = (best['tier'] == 'CUSTOMER', best['min_quantity'])
                if rank > best_rank:
                    best = t
            if best is None:
                continue
            is_customer = best['tier'] == 'CUSTOMER'
            entry = {
                'price': best['price'],
                'original_price': best['base_price'],
                'tier': best['tier'],
                'badge_text': 'سعر خاص ⭐' if is_customer else f'{sector_name} 🏢',
                'badge_color': '#ff7300' if is_customer else '#00e5ff',
                'price_list_name': best['price_list_name'],
            }
            if include_tiers:
                entry['tiers'] = product_tiers
            prices_map[pid] = entry
        return prices_map
