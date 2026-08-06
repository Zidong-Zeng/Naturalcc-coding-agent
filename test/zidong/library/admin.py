# 编写日期：2026年8月5日
# 作者：zidong
# 程序说明：图书管理系统 - 管理员类

from book import Book

# 管理员类：负责图书出入库管理工作
class Admin:
    def __init__(self, name="", pwd=""):
        self._username = name   # 管理员用户名
        self._password = pwd    # 管理员密码

    # 获取用户名
    def get_username(self):
        return self._username

    # 获取密码
    def get_password(self):
        return self._password

    # 图书入库：新增图书，或对已存在的图书增加库存
    # 在 books 中查找编号为 book_id 的图书，找到则加库存，否则新增一本
    @staticmethod
    def add_book(books, book_id, book_name, count):
        for b in books:
            if b.get_id() == book_id:
                b.in_stock(count)   # 已存在，增加库存
                print('入库成功："{}" 库存增加 {} 本，当前库存 {} 本。'.format(
                    book_name, count, b.get_stock()))
                return
        books.append(Book(book_id, book_name, count))   # 不存在，新增图书
        print('入库成功：新增图书 "{}" 共 {} 本。'.format(book_name, count))

    # 图书出库：减少库存，若库存不足或不存在返回 False
    @staticmethod
    def remove_book(books, book_id, count):
        if count < 0:
            return False
        for b in books:
            if b.get_id() == book_id:
                if b.out_stock(count):
                    print('出库成功："{}" 出库 {} 本，剩余库存 {} 本。'.format(
                        b.get_name(), count, b.get_stock()))
                    return True
                else:
                    print('出库失败：库存不足，当前库存仅 {} 本。'.format(b.get_stock()))
                    return False
        print('出库失败：未找到编号为 {} 的图书。'.format(book_id))
        return False
