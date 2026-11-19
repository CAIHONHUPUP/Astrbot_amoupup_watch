# -*- coding: utf-8 -*-
"""吃饭按地点：食堂只吃食堂套餐/面条，家里不出现食堂套餐，外面两样都不要。"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from services.daily_engine import CHEAP_MENUS, FinanceDaily  # noqa: E402


class Fake:
    where = ""

    def _where_text(self):
        return self.where

    _menu_for_place = FinanceDaily._menu_for_place
    _meal_zone = FinanceDaily._meal_zone


class MealZoneTests(unittest.TestCase):
    def test_canteen_only_canteen_and_noodles(self):
        f = Fake()
        f.where = "公司 在公司食堂吃午饭"
        menu = (("面条", 1), ("自己做饭", 1), ("食堂套餐", 1))
        got = f._menu_for_place("lunch", menu)
        names = [opt[0] for opt in got]
        self.assertIn("食堂套餐", names)
        self.assertIn("面条", names)
        self.assertNotIn("自己做饭", names)

    def test_canteen_with_schedule_place(self):
        f = Fake()
        f.where = "公司食堂 吃午饭"
        got = f._menu_for_place("lunch", CHEAP_MENUS["lunch"])
        names = [opt[0] for opt in got]
        self.assertNotIn("自己做饭", names)
        self.assertIn("食堂套餐", names)

    def test_outside_no_self_cook_no_canteen(self):
        f = Fake()
        f.where = "商场 和朋友吃饭"
        got = f._menu_for_place("dinner", CHEAP_MENUS["dinner"])
        names = [opt[0] for opt in got]
        self.assertNotIn("自己做饭", names)
        self.assertNotIn("食堂套餐", names)

    def test_home_keeps_self_cook_but_not_canteen(self):
        f = Fake()
        f.where = "家 在家做饭"
        got = f._menu_for_place("dinner", CHEAP_MENUS["dinner"])
        names = [opt[0] for opt in got]
        self.assertIn("自己做饭", names)
        self.assertNotIn("食堂套餐", names)

    def test_named_dish_wins(self):
        f = Fake()
        f.where = "商场 和他吃酸菜鱼"
        self.assertEqual(f._menu_for_place("dinner", CHEAP_MENUS["dinner"]),
                         (("酸菜鱼", 1),))

    def test_friend_named_dish_wins(self):
        f = Fake()
        f.where = "餐厅 朋友约我吃火锅"
        self.assertEqual(f._menu_for_place("lunch", CHEAP_MENUS["lunch"]),
                         (("火锅", 1),))

    def test_friend_without_dish_draws_restaurant(self):
        from services.daily_engine import DISH_WORDS
        f = Fake()
        f.where = "餐厅 朋友约我吃饭"
        got = f._menu_for_place("lunch", CHEAP_MENUS["lunch"])
        self.assertEqual(len(got), 1)
        self.assertIn(got[0][0], DISH_WORDS)

    def test_date_without_dish_waits_for_him(self):
        f = Fake()
        f.where = "商场 和他吃饭"
        names = [opt[0] for opt in f._menu_for_place("lunch", CHEAP_MENUS["lunch"])]
        self.assertNotIn("食堂套餐", names)
        self.assertNotIn("自己做饭", names)

    def test_unknown_place_keeps_menu(self):
        f = Fake()
        f.where = ""
        self.assertEqual(f._menu_for_place("lunch", CHEAP_MENUS["lunch"]), CHEAP_MENUS["lunch"])


if __name__ == "__main__":
    unittest.main()
