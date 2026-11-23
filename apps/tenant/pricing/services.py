"""
PricingEngineService:
Resolves effective selling price for products using a strict 3-tier hierarchy:
  1. Tier 1 (Highest Priority): Customer-specific active price list.
  2. Tier 2 (Secondary Priority): Sector-specific active price list (if customer belongs to a sector).
  3. Tier 3 (Base Default): Default product card sale price (or branch-specific sale price).
"""
from decimal import Decimal
from django.utils import timezone
from django.db.models import Q
from apps.tenant.pricing.models import PriceList, PriceListItem


class PricingEngineService:
    @classmethod
    def get_effective_price(
        cls,
        product,
        partner=None,
        branch=None,
        uom=None,
        quantity=Decimal('1'),
        as_of_date=None
    ) -> dict:
        """
        Calculates and returns the effective price for a product.

        Returns a dictionary with:
          - price: Decimal final unit price
          - tier: 'CUSTOMER' | 'SECTOR' | 'DEFAULT'
          - tier_display: Human-readable Arabic label of the price source
          - price_list_name: Name of the active price list applied (or None)
          - original_price: Decimal base price of the product
          - discount_amount: Decimal savings per unit compared to default price
          - discount_percentage: Decimal percentage saved
          - badge_color: Color hint for UI display
        """
        if as_of_date is None:
            as_of_date = timezone.now().date()

        quantity = Decimal(str(quantity or 1))
        if quantity <= Decimal('0'):
            quantity = Decimal('1')

        # Determine base product price (branch price if available, else product.sale_price)
        if branch and hasattr(product, 'get_price_for_branch'):
            base_price = Decimal(str(product.get_price_for_branch(branch) or 0))
        else:
            base_price = Decimal(str(getattr(product, 'sale_price', Decimal('0')) or 0))

        # ---------------------------------------------------------------------
        # TIER 1: Customer-Specific Price List (أولوية قصوى)
        # ---------------------------------------------------------------------
        if partner and getattr(partner, 'partner_type', None) in ['CUSTOMER', 'BOTH']:
            customer_lists_qs = PriceList.objects.filter(
                list_type=PriceList.CUSTOMER,
                partner=partner,
                is_active=True
            ).filter(
                Q(start_date__isnull=True) | Q(start_date__lte=as_of_date),
                Q(end_date__isnull=True) | Q(end_date__gte=as_of_date)
            )

            if customer_lists_qs.exists():
                item_qs = PriceListItem.objects.filter(
                    price_list__in=customer_lists_qs,
                    product=product,
                    min_quantity__lte=quantity
                )
                if uom:
                    uom_item = item_qs.filter(uom=uom).order_by('-min_quantity').first()
                    if uom_item:
                        matched_item = uom_item
                    else:
                        matched_item = item_qs.filter(uom__isnull=True).order_by('-min_quantity').first()
                else:
                    matched_item = item_qs.filter(uom__isnull=True).order_by('-min_quantity').first()
                    if not matched_item:
                        matched_item = item_qs.order_by('-min_quantity').first()

                if matched_item:
                    final_price = matched_item.calculate_price(base_price)
                    discount_amount = max(Decimal('0'), base_price - final_price)
                    discount_pct = ((discount_amount / base_price) * Decimal('100')).quantize(Decimal('0.01')) if base_price > 0 else Decimal('0')
                    return {
                        'price': final_price,
                        'tier': 'CUSTOMER',
                        'tier_display': f'سعر خاص للعميل ({partner.name})',
                        'price_list_name': matched_item.price_list.name,
                        'price_list_id': matched_item.price_list.id,
                        'original_price': base_price,
                        'discount_amount': discount_amount,
                        'discount_percentage': discount_pct,
                        'badge_color': '#ff7300',
                        'badge_text': 'سعر عميل خاص ⭐',
                        'badge_class': 'badge-orange',
                    }

        # ---------------------------------------------------------------------
        # TIER 2: Sector-Specific Price List (أولوية القطاع)
        # ---------------------------------------------------------------------
        partner_sector = getattr(partner, 'sector', None) if partner else None
        if partner_sector and getattr(partner_sector, 'is_active', True):
            sector_lists_qs = PriceList.objects.filter(
                list_type=PriceList.SECTOR,
                sector=partner_sector,
                is_active=True
            ).filter(
                Q(start_date__isnull=True) | Q(start_date__lte=as_of_date),
                Q(end_date__isnull=True) | Q(end_date__gte=as_of_date)
            )

            if sector_lists_qs.exists():
                item_qs = PriceListItem.objects.filter(
                    price_list__in=sector_lists_qs,
                    product=product,
                    min_quantity__lte=quantity
                )
                if uom:
                    uom_item = item_qs.filter(uom=uom).order_by('-min_quantity').first()
                    if uom_item:
                        matched_item = uom_item
                    else:
                        matched_item = item_qs.filter(uom__isnull=True).order_by('-min_quantity').first()
                else:
                    matched_item = item_qs.filter(uom__isnull=True).order_by('-min_quantity').first()
                    if not matched_item:
                        matched_item = item_qs.order_by('-min_quantity').first()

                if matched_item:
                    final_price = matched_item.calculate_price(base_price)
                    discount_amount = max(Decimal('0'), base_price - final_price)
                    discount_pct = ((discount_amount / base_price) * Decimal('100')).quantize(Decimal('0.01')) if base_price > 0 else Decimal('0')
                    return {
                        'price': final_price,
                        'tier': 'SECTOR',
                        'tier_display': f'سعر قطاع: {partner_sector.name}',
                        'price_list_name': matched_item.price_list.name,
                        'price_list_id': matched_item.price_list.id,
                        'original_price': base_price,
                        'discount_amount': discount_amount,
                        'discount_percentage': discount_pct,
                        'badge_color': '#00e5ff',
                        'badge_text': f'قطاع {partner_sector.name} 🏢',
                        'badge_class': 'badge-info',
                    }

        # ---------------------------------------------------------------------
        # TIER 3: Default Product Card Price (السعر الافتراضي للصنف)
        # ---------------------------------------------------------------------
        return {
            'price': base_price,
            'tier': 'DEFAULT',
            'tier_display': 'سعر الصنف الافتراضي',
            'price_list_name': None,
            'price_list_id': None,
            'original_price': base_price,
            'discount_amount': Decimal('0.00'),
            'discount_percentage': Decimal('0.00'),
            'badge_color': '#94a3b8',
            'badge_text': 'سعر افتراضي 📦',
            'badge_class': 'badge-secondary',
        }

    @classmethod
    def get_customer_price_map(cls, partner, branch=None, as_of_date=None) -> dict:
        """
        Returns a dictionary mapping product_id (str) -> {
            'price': float,
            'original_price': float,
            'tier': 'CUSTOMER' | 'SECTOR',
            'badge_text': str,
            'badge_color': str,
            'price_list_name': str
        }
        for all active items in the customer's direct or sector price lists.
        Tier 1 (Customer-specific) items override Tier 2 (Sector-specific) items.
        """
        if as_of_date is None:
            as_of_date = timezone.now().date()
        if not partner:
            return {}

        prices_map = {}

        # 1. Tier 2: Sector price lists
        partner_sector = getattr(partner, 'sector', None)
        if partner_sector and getattr(partner_sector, 'is_active', True):
            sector_lists = PriceList.objects.filter(
                list_type=PriceList.SECTOR,
                sector=partner_sector,
                is_active=True
            ).filter(
                Q(start_date__isnull=True) | Q(start_date__lte=as_of_date),
                Q(end_date__isnull=True) | Q(end_date__gte=as_of_date)
            )
            sector_items = PriceListItem.objects.filter(price_list__in=sector_lists).select_related('product', 'price_list').order_by('min_quantity')
            for item in sector_items:
                pid_str = str(item.product_id)
                if pid_str in prices_map and item.uom is not None:
                    continue
                base = item.product.get_price_for_branch(branch) if branch else item.product.sale_price
                calc = item.calculate_price(base)
                prices_map[pid_str] = {
                    'price': float(calc),
                    'original_price': float(base),
                    'tier': 'SECTOR',
                    'badge_text': f'{partner_sector.name} 🏢',
                    'badge_color': '#00e5ff',
                    'price_list_name': item.price_list.name
                }

        # 2. Tier 1: Customer direct price lists (overrides sector)
        if getattr(partner, 'partner_type', None) in ['CUSTOMER', 'BOTH']:
            cust_lists = PriceList.objects.filter(
                list_type=PriceList.CUSTOMER,
                partner=partner,
                is_active=True
            ).filter(
                Q(start_date__isnull=True) | Q(start_date__lte=as_of_date),
                Q(end_date__isnull=True) | Q(end_date__gte=as_of_date)
            )
            cust_items = PriceListItem.objects.filter(price_list__in=cust_lists).select_related('product', 'price_list').order_by('min_quantity')
            for item in cust_items:
                pid_str = str(item.product_id)
                if pid_str in prices_map and prices_map[pid_str]['tier'] == 'CUSTOMER' and item.uom is not None:
                    continue
                base = item.product.get_price_for_branch(branch) if branch else item.product.sale_price
                calc = item.calculate_price(base)
                prices_map[pid_str] = {
                    'price': float(calc),
                    'original_price': float(base),
                    'tier': 'CUSTOMER',
                    'badge_text': 'سعر خاص ⭐',
                    'badge_color': '#ff7300',
                    'price_list_name': item.price_list.name
                }

        return prices_map
