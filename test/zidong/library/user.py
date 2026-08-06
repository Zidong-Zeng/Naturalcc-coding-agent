# 编写日期：2026年8月5日
# 作者：zidong
# 程序说明：图书管理系统 - 用户类

# 用户类：普通用户，可借阅与归还图书
class User:
    def __init__(self, name="", pwd=""):
        self._username = name           # 用户名
        self._password = pwd            # 密码
        self._borrowed_ids = []         # 当前借阅的图书编号列表

    # 获取用户名
    def get_username(self):
        return self._username

    # 获取密码
    def get_password(self):
        return self._password

    # 借阅图书：记录借阅的图书编号，若成功返回 True
    def borrow_book(self, book_id):
        for borrowed in self._borrowed_ids:
            if borrowed == book_id:
                return False            # 已借过该书
        self._borrowed_ids.append(book_id)
        return True

    # 归还图书：移除借阅记录，若未借过返回 False
    def return_book(self, book_id):
        for i, borrowed in enumerate(self._borrowed_ids):
            if borrowed == book_id:
                self._borrowed_ids.pop(i)
                return True
        return False

    # 是否已借阅该书
    def has_borrowed(self, book_id):
        return book_id in self._borrowed_ids

    # 获取当前借阅的图书数量
    def borrowed_count(self):
        return len(self._borrowed_ids)

    # 获取所有已借阅的图书编号
    def get_borrowed_ids(self):
        return self._borrowed_ids
