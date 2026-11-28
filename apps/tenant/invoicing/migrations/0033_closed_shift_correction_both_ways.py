from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):
    """The closed-shift payment correction now works both ways: recorded cash / really card-wallet-transfer
    (against the shift's shortage) and recorded card-wallet-transfer / really cash (against its overage)."""

    dependencies = [
        ('accounting', '0045_voucher_created_by'),
        ('invoicing', '0032_closed_shift_payment_correction'),
    ]

    operations = [
        migrations.RenameField('closedshiftpaymentcorrection', 'shortage_account', 'variance_account'),
        migrations.AlterField(
            model_name='closedshiftpaymentcorrection',
            name='variance_account',
            field=models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='+',
                                    to='accounting.account',
                                    verbose_name='حساب العجز / الزيادة (من قيد قفل الوردية)'),
        ),
        migrations.AddField(
            model_name='closedshiftpaymentcorrection',
            name='recorded_method',
            field=models.CharField(choices=[('CASH', 'نقدي'), ('CARD', 'فيزا'), ('EWALLET', 'محفظة إلكترونية'),
                                            ('BANK_TRANSFER', 'تحويل بنكي / إنستاباي')],
                                   default='CASH', max_length=20, verbose_name='الطريقة اللي اتسجلت'),
        ),
        migrations.AlterField(
            model_name='closedshiftpaymentcorrection',
            name='method',
            field=models.CharField(choices=[('CASH', 'نقدي'), ('CARD', 'فيزا'), ('EWALLET', 'محفظة إلكترونية'),
                                            ('BANK_TRANSFER', 'تحويل بنكي / إنستاباي')],
                                   max_length=20, verbose_name='الطريقة الصح'),
        ),
    ]
