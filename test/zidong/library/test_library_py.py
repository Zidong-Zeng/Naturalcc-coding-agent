# 编写日期：2026年8月5日
# 作者：zidong
# 程序说明：图书管理系统 - Python 自动化测试用例
# 覆盖 Book / User / Admin 三个类的基本功能

from book import Book
from user import User
from admin import Admin

passed = 0
failed = 0


def check(name, actual, expected):
    global passed, failed
    if actual == expected:
        passed += 1
        print("[PASS] {}".format(name))
    else:
        failed += 1
        print("[FAIL] {} - 期望: {}, 实际: {}".format(name, expected, actual))


def check_true(name, actual):
    check(name, actual, True)


def check_false(name, actual):
    check(name, actual, False)


# ============ 用例一：Book 图书类 ============
print("===== 用例一：Book 图书类 =====")
b = Book("B001", "C++入门", 3)
check("初始库存", b.get_stock(), 3)
check("初始编号", b.get_id(), "B001")
check("初始书名", b.get_name(), "C++入门")

# 入库
b.in_stock(2)
check("入库后库存 3+2=5", b.get_stock(), 5)

# 借阅一本
check_true("借阅成功 (库存5→4)", b.borrow_one())
check("借阅后库存", b.get_stock(), 4)

# 出库超限
check_false("出库超过库存应返回False", b.out_stock(10))
check("超限出库后库存不变", b.get_stock(), 4)

# 归还
b.return_one()
check("归还后库存 4→5", b.get_stock(), 5)

# 负数出库
check_false("负数出库应返回False", b.out_stock(-1))
check("负数出库后库存不变", b.get_stock(), 5)

# ============ 用例二：User 用户类 ============
print("===== 用例二：User 用户类 =====")
u = User("张三", "123456")
check("用户名", u.get_username(), "张三")
check("密码", u.get_password(), "123456")
check("初始借阅数", u.borrowed_count(), 0)

# 借阅去重
check_true("借阅 B001", u.borrow_book("B001"))
check_false("重复借阅 B001 应被拒绝", u.borrow_book("B001"))
check_true("借阅 B002", u.borrow_book("B002"))
check("借阅两本后数量", u.borrowed_count(), 2)

# 查重
check_true("has_borrowed B001", u.has_borrowed("B001"))
check_false("has_borrowed B999(未借过)", u.has_borrowed("B999"))

# 归还
check_true("归还 B001", u.return_book("B001"))
check_false("再次归还 B001 应被拒绝", u.return_book("B001"))
check_false("归还未借过的 B999 应被拒绝", u.return_book("B999"))
check("归还后数量", u.borrowed_count(), 1)
check("剩余借阅书目", u.get_borrowed_ids(), ["B002"])

# ============ 用例三：Admin 管理员出入库 ============
print("===== 用例三：Admin 管理员出入库 =====")
books = []
Admin.add_book(books, "B001", "C++入门", 5)
check("入库后图书数量", len(books), 1)
check("入库后 B001 库存", books[0].get_stock(), 5)

# 重复入库（不新增，合并库存）
Admin.add_book(books, "B001", "C++入门", 3)
check("重复入库后图书数量仍为1", len(books), 1)
check("重复入库后库存 5+3=8", books[0].get_stock(), 8)

# 新增另一本
Admin.add_book(books, "B002", "数据结构", 2)
check("新增第二本后图书数量", len(books), 2)
check("B002 库存", books[1].get_stock(), 2)

# 正常出库
check_true("B001 出库 3 本", Admin.remove_book(books, "B001", 3))
check("出库后 B001 库存 8-3=5", books[0].get_stock(), 5)

# 出库超限
check_false("B002 出库 99 本(不足)应失败", Admin.remove_book(books, "B002", 99))
check("超限出库后 B002 库存不变", books[1].get_stock(), 2)

# 出不存在的书
check_false("出库不存在的 B999 应失败", Admin.remove_book(books, "B999", 1))

# 负数出库
check_false("负数出库应失败", Admin.remove_book(books, "B001", -1))

# ============ 用例四：综合场景（入库→借阅→归还） ============
print("===== 用例四：综合场景 =====")
books2 = []
user2 = User("李四", "123456")

# 管理员入库 B100 两本
Admin.add_book(books2, "B100", "Python入门", 2)
check("B100 入库库存=2", books2[0].get_stock(), 2)

# 用户借阅一本
b100 = books2[0]
check_true("用户借阅 B100", b100.borrow_one())
check("借阅后库存 2→1", b100.get_stock(), 1)
check_true("User 记录借阅 B100", user2.borrow_book("B100"))
check("用户借阅数=1", user2.borrowed_count(), 1)

# 用户归还
b100.return_one()
check("归还后库存 1→2", b100.get_stock(), 2)
check_true("User 归还 B100", user2.return_book("B100"))
check("用户归还后借阅数=0", user2.borrowed_count(), 0)

# ============ 汇总 ============
print("=" * 30)
print("测试完成：通过 {} 项，失败 {} 项".format(passed, failed))
if failed == 0:
    print(">>> 全部用例 PASS <<<")
else:
    print(">>> 存在失败用例 <<<")
