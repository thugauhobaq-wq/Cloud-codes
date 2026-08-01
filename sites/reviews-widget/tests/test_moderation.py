import unittest

from widget.models import Review, Site
from widget.moderation import RateLimiter, check_submission, check_text, decide_status


class SpamTest(unittest.TestCase):
    def test_normal_review_passes(self):
        verdict = check_text("Заказывал дважды, привезли вовремя. Персонал вежливый.")
        self.assertTrue(verdict.ok)
        self.assertEqual(verdict.flags, [])

    def test_link_plus_spam_words_blocked(self):
        verdict = check_text("Лучший заработок! Заходи http://casino.example и получай крипто")
        self.assertFalse(verdict.ok)
        self.assertIn("links", verdict.flags)
        self.assertIn("spam-words", verdict.flags)

    def test_single_link_is_flagged_but_not_blocked(self):
        verdict = check_text("Заказывал тут же на www.example.ru, всё пришло целым и в срок")
        self.assertTrue(verdict.ok)
        self.assertIn("links", verdict.flags)

    def test_caps_and_repeats_flagged(self):
        self.assertIn("repeats", check_text("Ужааааааасно, не советую никому вообще").flags)
        self.assertIn("caps", check_text("ОЧЕНЬ ПЛОХОЙ СЕРВИС НИКОМУ НЕ СОВЕТУЮ ВООБЩЕ").flags)

    def test_spam_words_match_word_starts_not_substrings(self):
        # «доставка» содержит «ставк» — на этом ловились обычные отзывы
        # маркетплейсов, где про доставку пишет чуть ли не каждый второй.
        for text in (
            "Быстрая доставка, курьер приехал раньше срока. Упаковка целая",
            "Доставили вовремя, доставка бесплатная при заказе от двух тысяч",
            "Перестановка мебели заняла час, всё аккуратно",
        ):
            verdict = check_text(text)
            self.assertTrue(verdict.ok, text)
            self.assertNotIn("spam-words", verdict.flags, text)

    def test_real_spam_stems_still_caught(self):
        for text in (
            "Ставки на спорт каждый день, пишите в личку за прогнозами",
            "Даём займы без отказа, оформление за пять минут",
            "Накрутка подписчиков дёшево, обращайтесь",
        ):
            self.assertIn("spam-words", check_text(text).flags, text)

    def test_repetitive_word_soup(self):
        verdict = check_text("купить купить купить дёшево купить купить дёшево купить купить")
        self.assertIn("repetitive", verdict.flags)


class SubmissionTest(unittest.TestCase):
    def test_honeypot_blocks(self):
        verdict = check_submission({"website": "http://bot.ru", "text": "ок"})
        self.assertFalse(verdict.ok)
        self.assertIn("honeypot", verdict.flags)

    def test_too_fast_blocks(self):
        self.assertFalse(check_submission({"elapsed": 0.4}).ok)

    def test_human_pace_passes(self):
        self.assertTrue(check_submission({"elapsed": 25}).ok)

    def test_missing_elapsed_is_not_fatal(self):
        # Отзыв может прийти из бота или импорта, где времени заполнения нет.
        self.assertTrue(check_submission({}).ok)


class RateLimiterTest(unittest.TestCase):
    def test_minute_window(self):
        limiter = RateLimiter(per_minute=2, per_hour=10)
        self.assertTrue(limiter.allow("ip", now=1000))
        self.assertTrue(limiter.allow("ip", now=1001))
        self.assertFalse(limiter.allow("ip", now=1002))
        # Через минуту окно уезжает.
        self.assertTrue(limiter.allow("ip", now=1075))

    def test_hour_cap(self):
        limiter = RateLimiter(per_minute=100, per_hour=3)
        for index in range(3):
            self.assertTrue(limiter.allow("ip", now=1000 + index * 120))
        self.assertFalse(limiter.allow("ip", now=1400))

    def test_keys_are_independent(self):
        limiter = RateLimiter(per_minute=1, per_hour=5)
        self.assertTrue(limiter.allow("a", now=10))
        self.assertTrue(limiter.allow("b", now=10))


class AutoApproveTest(unittest.TestCase):
    def setUp(self):
        self.site = Site.create("Тест", auto_approve=True, auto_approve_from=4)

    def make(self, text, rating):
        return Review.build(self.site.key, text, rating)

    def test_clean_high_rating_published(self):
        review = self.make("Всё понравилось, приеду ещё раз обязательно", 5)
        self.assertEqual(decide_status(self.site, review, check_text(review.text)), "published")

    def test_low_rating_goes_to_moderation(self):
        review = self.make("Ждал слишком долго, больше не приду", 2)
        self.assertEqual(decide_status(self.site, review, check_text(review.text)), "pending")

    def test_flagged_text_goes_to_moderation_even_with_five_stars(self):
        review = self.make("Отлично, пишите мне на www.example.ru за скидкой", 5)
        self.assertEqual(decide_status(self.site, review, check_text(review.text)), "pending")

    def test_auto_approve_off_keeps_everything_pending(self):
        site = Site.create("Тест")
        review = self.make("Всё понравилось, приеду ещё раз обязательно", 5)
        self.assertEqual(decide_status(site, review, check_text(review.text)), "pending")


if __name__ == "__main__":
    unittest.main()
