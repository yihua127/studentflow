"""Shared SplitText-style reveal for workspace headings."""

from html import escape

import streamlit as st


def _split_text(text: str) -> str:
    index = 0
    words = []
    for word in text.split():
        letters = []
        for letter in word:
            letters.append(
                f'<span class="sf-heading-letter" style="--sf-letter-index:{index}">{escape(letter)}</span>'
            )
            index += 1
        words.append('<span class="sf-heading-word">' + ''.join(letters) + '</span>')
    return (
        f'<span class="sf-heading-original">{escape(text)}</span>'
        f'<span aria-hidden="true">{" ".join(words)}</span>'
    )


def render_page_heading(eyebrow: str, title: str, description: str) -> None:
    st.html(
        '<header class="sf-page-heading">'
        f'<h1>{_split_text(title)}</h1>'
        f'<p class="sf-page-heading-description">{_split_text(description)}</p>'
        f'<div class="sf-eyebrow">{_split_text(eyebrow)}</div>'
        '</header>'
    )
