"""The two signed-out password screens: ask for a link, and set a password from one. 404 unless the feature is on."""

# Lazy annotations, like the rest of the codebase.
from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, Response

from setuhaul.auth import local_admin
from setuhaul.auth.pages import _message_page, _respond, _secret_field, _signin_frame
from setuhaul.auth.password_routes import FORGOT_REPLY

if TYPE_CHECKING:
    from setuhaul.api import AppDeps

_BACK = '<p><a href="/">Back to sign in</a></p>'
_STATUS = '<p id="status" class="status" role="alert"></p>'


def _forgot_page() -> str:
    # method="post" keeps the typed values out of the address if the script does not run.
    return _signin_frame("Forgot password", '<h2>Forgot password</h2>'
                         '<p class="lead">We will email you a link to set a new one.</p>' + _STATUS
                         + f'<form method="post" data-api="/auth/password/forgot" data-done="{FORGOT_REPLY}">'
                         '<label for="email">Email</label><input id="email" name="email" type="email" required autocomplete="username">'
                         '<button type="submit">Send the link</button></form>' + _BACK)


def _set_page() -> str:
    rules = f'minlength="{local_admin.MIN_LENGTH}" maxlength="{local_admin.MAX_LENGTH}" autocomplete="new-password"'
    # The token is filled in by password.js from the part of the address after "#", which is never sent to the server.
    return _signin_frame("Set password", '<h2>Set your password</h2>'
                         f'<p class="lead">Use at least {local_admin.MIN_LENGTH} characters.</p>' + _STATUS
                         + '<form method="post" data-api="/auth/password/set" data-next="/?signin=password_set">'
                         '<input type="hidden" name="token" id="token">'
                         + _secret_field("password", "New password", f'name="password" {rules}')
                         # No name, so it is checked here and never sent.
                         + _secret_field("repeat", "New password again", rules)
                         + '<button type="submit">Save password</button></form>' + _BACK,
                         ("password.js", "common.js"))


def register_password_pages(app: FastAPI, deps: AppDeps) -> None:
    def page(body: str) -> Response:
        if not deps.password_login_on:
            return _message_page("Not found", "There is no such page.", 404)
        return _respond(body)

    @app.get("/forgot-password", response_class=HTMLResponse)
    def forgot_password_page() -> Response:
        return page(_forgot_page())

    @app.get("/set-password", response_class=HTMLResponse)
    def set_password_page() -> Response:
        return page(_set_page())
