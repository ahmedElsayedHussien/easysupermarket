"""
ضريبة كسب العمل والتأمينات الاجتماعية في المسير (hr.PayrollTaxSettings).

  insurable wage   employee.insurance_wage (or base salary), kept between the min / max
  employee share   insurable wage x employee rate        (deducted from the salary)
  employer share   insurable wage x employer rate        (a cost of the company: 5212)
  taxable (year)   (gross - employee share) x 12 - personal exemption
  tax (month)      progressive brackets on the yearly amount / 12
All Decimal, rounded to the piastre.
"""
from decimal import Decimal
from apps.tenant.core.money import money

CENT = Decimal('0.01')
ZERO = Decimal('0')


def annual_tax(taxable, brackets):
    """Progressive tax on a yearly amount: brackets = [[upper or None, rate %], ...]."""
    taxable = max(Decimal(taxable), ZERO)
    tax, lower = ZERO, ZERO
    for upper, rate in brackets:
        upper = None if upper in (None, '') else Decimal(str(upper))
        if taxable <= lower:
            break
        top = taxable if upper is None else min(taxable, upper)
        tax += (top - lower) * Decimal(str(rate)) / 100
        if upper is None:
            break
        lower = upper
    return tax


def compute(employee, gross, settings):
    """{'social_insurance', 'employer_insurance', 'income_tax'} for a month's gross salary."""
    out = {'social_insurance': ZERO, 'employer_insurance': ZERO, 'income_tax': ZERO}
    if settings.insurance_enabled and employee.is_insured:
        wage = employee.insurance_wage if employee.insurance_wage else employee.base_salary
        wage = min(max(Decimal(wage or 0), settings.insurance_min_wage), settings.insurance_max_wage)
        out['social_insurance'] = money(wage * settings.employee_insurance_rate / 100)
        out['employer_insurance'] = money(wage * settings.employer_insurance_rate / 100)
    if settings.tax_enabled and not employee.tax_exempt and gross > 0:
        yearly = (gross - out['social_insurance']) * 12 - settings.personal_exemption
        out['income_tax'] = money(annual_tax(yearly, settings.brackets) / 12)
    return out
