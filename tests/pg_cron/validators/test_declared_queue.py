def test_undeclared_queue_override_rejected(validate):
    # explicit override: model clean() and core both reject an undeclared queue.
    # (The no-override, task-intrinsic-queue branch is covered by
    # test_pg_cron_undeclared_task_queue_rejected and every valid-baseline model test.)
    result = validate(queue="ghost")
    assert result
    assert "queue 'ghost' is not declared." in result


def test_bad_task_no_queue_reports_task_not_queue(validate):
    # no override + unimportable/not-a-task path: validate_declared_queue must SWALLOW
    # the task error (reported by validate_task_path) and not mislabel it as a queue
    # error. Exercises the try/except-return branch on both subjects.
    result = validate(task="os.getpid")
    assert result
    assert "is not a Django task." in result
    assert "is not declared" not in result
