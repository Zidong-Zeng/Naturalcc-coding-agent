# 编写日期：2026年8月5日
# 作者：zidong
# 程序说明：图书管理系统 - 图书类

# 图书类：记录图书编号、书名、库存数量
class Book:
    def __init__(self, book_id="", book_name="", book_stock=0):
        self._id = book_id        # 图书编号
        self._name = book_name    # 书名
        self._stock = book_stock  # 当前库存数量

    # 获取图书编号
    def get_id(self):
        return self._id

    # 获取书名
    def get_name(self):
        return self._name

    # 获取库存数量
    def get_stock(self):
        return self._stock

    # 入库：库存增加 count 本
    def in_stock(self, count):
        self._stock += count

    # 出库：库存减少 count 本，若数量不足返回 False
    def out_stock(self, count):
        if count < 0 or count > self._stock:
            return False
        self._stock -= count
        return True

    # 借阅：库存减少 1 本，若无库存返回 False
    def borrow_one(self):
        return self.out_stock(1)

    # 归还：库存增加 1 本
    def return_one(self):
        self.in_stock(1)

    # 显示图书信息
    def print(self):
        print("编号: {} | 书名: {} | 库存: {}".format(self._id, self._name, self._stock))
