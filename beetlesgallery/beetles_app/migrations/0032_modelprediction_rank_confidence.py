from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("beetles_app", "0031_game_granted_perks"),
    ]

    operations = [
        migrations.AddField(
            model_name="modelprediction",
            name="rank_confidence",
            field=models.JSONField(
                blank=True, default=dict,
                help_text='What the model said per rank, when it says so: {"genus": {"value": "Xyleborus", "confidence": 0.92}, ...}',
            ),
        ),
    ]
