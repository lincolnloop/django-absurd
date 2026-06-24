from django.core.management.base import BaseCommand

from demo.tasks import add, create_user, create_user_async


class Command(BaseCommand):
    help = "Enqueue the demo tasks onto the default Absurd queue."

    def handle(self, *args: object, **options: object) -> None:
        add_result = add.enqueue(2, 3)
        user_result = create_user.enqueue("alice")
        async_result = create_user_async.enqueue("alice-async")
        self.stdout.write(f"Enqueued add(2, 3) -> task {add_result.id}")
        self.stdout.write(f"Enqueued create_user('alice') -> task {user_result.id}")
        self.stdout.write(
            f"Enqueued create_user_async('alice-async') -> task {async_result.id}"
        )
        self.stdout.write(
            "Run a worker to execute them:"
            " manage.py absurd_worker --queue default --burst"
        )
