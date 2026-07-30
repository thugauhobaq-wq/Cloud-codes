import unittest

from widget.models import (
    Review,
    Site,
    ValidationError,
    clean_text,
    hash_token,
    initials,
    normalize_domains,
    normalize_settings,
    parse_rating,
    verify_token,
)


class CleanTextTest(unittest.TestCase):
    def test_strips_tags_and_entities(self):
        self.assertEqual(clean_text("<b>Привет</b>&nbsp;мир", 100), "Привет мир")

    def test_drops_zero_width_and_control(self):
        dirty = "Хоро​ший сервис"
        self.assertEqual(clean_text(dirty, 100), "Хороший сервис")

    def test_script_tag_cannot_survive(self):
        self.assertNotIn("<", clean_text("<script>alert(1)</script>Отзыв", 200))

    def test_keeps_paragraphs_but_squeezes_blank_lines(self):
        self.assertEqual(clean_text("а\n\n\n\nб", 100), "а\n\nб")

    def test_respects_limit(self):
        self.assertEqual(len(clean_text("я" * 500, 20)), 20)


class RatingTest(unittest.TestCase):
    def test_accepts_numbers_and_strings(self):
        self.assertEqual(parse_rating("4"), 4)
        self.assertEqual(parse_rating(4.4), 4)
        self.assertEqual(parse_rating("4,6"), 5)

    def test_rejects_out_of_range(self):
        for value in (0, 6, -1, "восемь", None):
            with self.assertRaises(ValidationError):
                parse_rating(value)


class ReviewTest(unittest.TestCase):
    def test_build_normalizes_fields(self):
        review = Review.build("s_1", "  Отличный <i>сервис</i>, спасибо!  ", "5", " Анна ")
        self.assertEqual(review.text, "Отличный сервис, спасибо!")
        self.assertEqual(review.author, "Анна")
        self.assertEqual(review.rating, 5)
        self.assertEqual(review.status, "pending")

    def test_short_text_rejected(self):
        with self.assertRaises(ValidationError):
            Review.build("s_1", "норм", 5)

    def test_empty_author_becomes_anonymous(self):
        self.assertEqual(Review.build("s_1", "Хороший сервис", 5).author, "Аноним")

    def test_link_in_author_rejected(self):
        with self.assertRaises(ValidationError):
            Review.build("s_1", "Хороший сервис", 5, author="http://spam.ru")

    def test_uid_is_stable_and_text_sensitive(self):
        first = Review.build("s_1", "Хороший сервис, рекомендую", 5, "Аня")
        same = Review.build("s_1", "Хороший сервис, рекомендую!", 4, "Аня")
        other = Review.build("s_1", "Другой отзыв целиком", 5, "Аня")
        # Пунктуация и оценка на отпечаток не влияют, текст — влияет.
        self.assertEqual(first.uid, same.uid)
        self.assertNotEqual(first.uid, other.uid)

    def test_public_hides_contact(self):
        review = Review.build("s_1", "Хороший сервис", 5, "Аня", contact="a@b.ru")
        self.assertNotIn("contact", review.public())
        self.assertEqual(review.admin()["contact"], "a@b.ru")

    def test_initials(self):
        self.assertEqual(initials("Анна Петрова"), "АП")
        self.assertEqual(initials("Дмитрий"), "ДМ")
        self.assertEqual(initials(""), "?")


class SettingsTest(unittest.TestCase):
    def test_unknown_keys_ignored(self):
        settings = normalize_settings({"theme": "dark", "чужое": 1})
        self.assertEqual(settings["theme"], "dark")
        self.assertNotIn("чужое", settings)

    def test_numbers_clamped(self):
        settings = normalize_settings({"limit": 9999, "interval": 10, "radius": -5})
        self.assertEqual(settings["limit"], 100)
        self.assertEqual(settings["interval"], 2000)
        self.assertEqual(settings["radius"], 0)

    def test_bad_enum_and_colour_fall_back(self):
        settings = normalize_settings({"theme": "неон", "accent": "javascript:alert(1)"})
        self.assertEqual(settings["theme"], "auto")
        self.assertEqual(settings["accent"], "#2f6df6")

    def test_colour_accepted(self):
        self.assertEqual(normalize_settings({"accent": "#FF0044"})["accent"], "#ff0044")

    def test_checkbox_strings_become_bools(self):
        settings = normalize_settings({"autoplay": "false", "show_summary": "on"})
        self.assertIs(settings["autoplay"], False)
        self.assertIs(settings["show_summary"], True)

    def test_accepts_json_string(self):
        self.assertEqual(normalize_settings('{"layout": "grid"}')["layout"], "grid")


class TokenTest(unittest.TestCase):
    def test_hash_is_salted_and_verifies(self):
        token = "секретный-токен"
        first, second = hash_token(token), hash_token(token)
        self.assertNotEqual(first, second)          # соль у каждого своя
        self.assertTrue(first.startswith("sha256$"))
        self.assertNotIn(token, first)
        self.assertTrue(verify_token(first, token))
        self.assertTrue(verify_token(second, token))

    def test_wrong_token_rejected(self):
        stored = hash_token("правильный")
        for value in ("неправильный", "", "правильный "):
            self.assertFalse(verify_token(stored, value))

    def test_empty_and_broken_hashes_reject(self):
        self.assertFalse(verify_token("", "что-то"))
        self.assertFalse(verify_token("sha256$сломано", "что-то"))

    def test_legacy_plaintext_still_verifies(self):
        # Базы первых версий хранили токен как есть — доступ терять нельзя.
        self.assertTrue(verify_token("открытый-токен", "открытый-токен"))
        self.assertFalse(verify_token("открытый-токен", "другой"))

    def test_site_create_keeps_plain_token_out_of_hash(self):
        site = Site.create("Тест")
        self.assertTrue(site.plain_token)
        self.assertNotIn(site.plain_token, site.token_hash)
        self.assertTrue(site.check_token(site.plain_token))
        self.assertFalse(site.token_needs_upgrade())

    def test_rotate_invalidates_old_token(self):
        site = Site.create("Тест")
        old = site.plain_token
        new = site.rotate_token()
        self.assertNotEqual(old, new)
        self.assertFalse(site.check_token(old))
        self.assertTrue(site.check_token(new))

    def test_legacy_site_wants_upgrade(self):
        site = Site.create("Тест")
        site.token_hash = "открытый-токен"
        self.assertTrue(site.token_needs_upgrade())
        self.assertTrue(site.check_token("открытый-токен"))


class SiteTest(unittest.TestCase):
    def test_domains_normalized(self):
        domains = normalize_domains("https://Example.ru/, www.example.ru , example.ru")
        self.assertEqual(domains, ["example.ru", "www.example.ru"])

    def test_origin_check(self):
        site = Site.create("Тест", domains=["example.ru"])
        self.assertTrue(site.allows_origin("https://example.ru"))
        self.assertTrue(site.allows_origin("https://shop.example.ru:8443"))
        self.assertFalse(site.allows_origin("https://example.ru.evil.com"))
        self.assertFalse(site.allows_origin("https://other.ru"))

    def test_empty_domains_allow_everything(self):
        self.assertTrue(Site.create("Тест").allows_origin("https://any.site"))

    def test_key_and_token_are_unique(self):
        first, second = Site.create("А"), Site.create("Б")
        self.assertNotEqual(first.key, second.key)
        self.assertNotEqual(first.plain_token, second.plain_token)
        self.assertNotEqual(first.token_hash, second.token_hash)


if __name__ == "__main__":
    unittest.main()
