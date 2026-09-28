"""A failed or interrupted listing must never look like a complete account scan."""

from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from dropbox.exceptions import ApiError, RateLimitError
from dropbox.files import ListFolderError, LookupError

from dbxpull.config import Config
from dbxpull.models import FilterOptions
from dbxpull.scanner import ScanError, scan_dropbox
from tests.test_integrity import metadata


def test_paginated_scan_retries_rate_limit_and_applies_filters():
    dbx = Mock()
    dbx.files_list_folder.return_value = SimpleNamespace(
        entries=[metadata(path="/first")], has_more=True, cursor="cursor",
    )
    dbx.files_list_folder_continue.side_effect = [
        RateLimitError("test", backoff=0),
        SimpleNamespace(entries=[metadata(path="/second"),
                                 metadata(path="/project/node_modules/dependency")], has_more=False),
    ]
    files, deps, other = scan_dropbox(
        dbx, "/", FilterOptions(), Config(max_retries=2, backoff_base=0, backoff_max=0),
    )
    assert [f.path_display for f in files] == ["/first", "/second"]
    assert (deps, other) == (1, 0)
    assert dbx.files_list_folder_continue.call_count == 2


@pytest.mark.parametrize("continuation", [False, True])
def test_api_error_aborts_scan_without_success_message(capsys, continuation):
    dbx = Mock()
    error = ApiError("test", ListFolderError.path(LookupError.not_found), None, None)
    if continuation:
        dbx.files_list_folder.return_value = SimpleNamespace(
            entries=[metadata()], has_more=True, cursor="cursor",
        )
        dbx.files_list_folder_continue.side_effect = error
    else:
        dbx.files_list_folder.side_effect = error
    with pytest.raises(ScanError):
        scan_dropbox(dbx, "", FilterOptions(), Config())
    assert "Scan complete!" not in capsys.readouterr().out


def test_scan_cancellation_before_request():
    dbx, stop = Mock(), Event()
    stop.set()
    with pytest.raises(InterruptedError):
        scan_dropbox(dbx, "", FilterOptions(), Config(), stop)
    dbx.files_list_folder.assert_not_called()


def test_scan_cancellation_during_backoff():
    dbx, stop = Mock(), Event()

    def limited(*args, **kwargs):
        stop.set()
        raise RateLimitError("test", backoff=300)

    dbx.files_list_folder.side_effect = limited
    with pytest.raises(InterruptedError):
        scan_dropbox(dbx, "", FilterOptions(), Config(), stop)
    assert dbx.files_list_folder.call_count == 1


@pytest.mark.parametrize("setting,value", [("max_retries", 0), ("chunk_size", 0),
                                           ("max_gb_per_run", -1)])
def test_invalid_download_settings_are_rejected(tmp_path, setting, value):
    config = Config(access_token="test", dest_root=str(tmp_path))
    setattr(config, setting, value)
    assert any(setting in error for error in config.validate())
