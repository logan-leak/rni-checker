def _normalize_month(text: str) -> str:
    return re.sub(r"[^a-z]", "", (text or "").strip().lower())


def _month_to_num(text: str) -> Optional[int]:
    t = _normalize_month(text)
    if not t:
        return None

    # full names
    if t in MONTH_NAME_TO_NUM:
        return MONTH_NAME_TO_NUM[t]

    # abbreviations like "jul", "aug"
    for name, num in MONTH_NAME_TO_NUM.items():
        if t == name[:3]:
            return num

    return None


def _month_aliases(month_idx: int) -> list[str]:
    en = MONTH_NAMES_EN[month_idx - 1]
    nl = MONTH_NAMES_NL[month_idx - 1]
    return [en, en[:3], en.title(), en[:3].title(), nl, nl[:3], nl.title(), nl[:3].title()]


def _find_month_year_selects(page):
    selects = page.locator("select")
    if selects.count() < 2:
        return None, None

    month_select = None
    year_select = None

    for i in range(selects.count()):
        sel = selects.nth(i)
        option_texts = [(_normalize_month(t) or "") for t in _option_texts(sel)]
        joined = " ".join(option_texts)

        if month_select is None and any(m[:3] in joined for m in MONTH_NAMES_EN + MONTH_NAMES_NL):
            month_select = sel
            continue

        if year_select is None and any(re.fullmatch(r"20\d{2}", t) for t in option_texts):
            year_select = sel
            continue

    if month_select is None:
        month_select = selects.nth(0)
    if year_select is None and selects.count() > 1:
        year_select = selects.nth(1)

    return month_select, year_select


def _select_option_by_text(select_locator, target_texts: list[str]) -> bool:
    target_set = {_normalize_month(t) for t in target_texts if t and _normalize_month(t)}

    try:
        options = select_locator.locator("option")
        for i in range(options.count()):
            option = options.nth(i)
            text = _normalize_month(option.inner_text())
            value = _normalize_month(option.get_attribute("value") or "")

            for target in target_set:
                if text == target or text.startswith(target) or target.startswith(text) or value == target:
                    select_locator.select_option(index=i)
                    return True
    except Exception:
        pass

    for txt in target_texts:
        try:
            select_locator.select_option(label=txt)
            return True
        except Exception:
            pass

    return False


def get_visible_month_year(page) -> Optional[tuple[int, int]]:
    try:
        month_select, year_select = _find_month_year_selects(page)
        if month_select is not None and year_select is not None:
            month_text = _normalize_month(
                month_select.locator("option:checked").inner_text()
            )
            year_text = year_select.locator("option:checked").inner_text().strip()

            month_num = _month_to_num(month_text)
            if month_num and year_text.isdigit():
                return month_num, int(year_text)
    except Exception:
        pass

    return None


def select_target_month_year(page, target_month: int, target_year: int) -> bool:
    month_select, year_select = _find_month_year_selects(page)
    if month_select is None or year_select is None:
        return False

    ok_month = _select_option_by_text(month_select, _month_aliases(target_month))
    if not ok_month:
        return False

    ok_year = _select_option_by_text(year_select, [str(target_year)])
    if not ok_year:
        return False

    page.wait_for_timeout(1500)
    return True


def navigate_to_target_month(page, target_month: int, target_year: int, max_clicks: int = 24) -> bool:
    current = get_visible_month_year(page)
    if current == (target_month, target_year):
        return True

    if select_target_month_year(page, target_month, target_year):
        page.wait_for_timeout(1000)
        return True

    next_selectors = [
        'button:has-text("Next month")',
        'button:has-text("Volgende maand")',
        'button[aria-label*="next" i]',
        'button[aria-label*="volgende" i]',
        'a[aria-label*="next" i]',
        'a[aria-label*="volgende" i]',
        'button:has-text(">")',
    ]

    for _ in range(max_clicks):
        current = get_visible_month_year(page)
        if current == (target_month, target_year):
            return True

        clicked = False
        for sel in next_selectors:
            try:
                locator = page.locator(sel)
                if locator.count() > 0 and locator.first.is_visible():
                    locator.first.click(timeout=2000)
                    page.wait_for_timeout(900)
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            break

    return get_visible_month_year(page) == (target_month, target_year)