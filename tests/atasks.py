from django.tasks import task


@task
async def aecho(value):
    return value
