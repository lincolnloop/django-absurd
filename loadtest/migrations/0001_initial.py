from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True
    dependencies = []

    operations = [
        migrations.CreateModel(
            name="ExecutionLog",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("task_id", models.UUIDField()),
                ("pid", models.IntegerField()),
                ("logged_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
    ]
