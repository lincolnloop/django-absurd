import importlib.resources

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("django_absurd", "0001_initial_0_5_0")]

    operations = [
        migrations.RunSQL(
            sql=importlib.resources.files("django_absurd.migrations")
            .joinpath("0002_absurd_0_5_1.sql")
            .read_text(encoding="utf-8"),
        ),
    ]
