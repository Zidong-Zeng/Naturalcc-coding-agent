# 编写日期：2026年8月5日
# 作者：zidong
# 程序说明：图书管理系统 - 主程序（注册/登录 + 菜单交互）

from book import Book
from user import User
from admin import Admin


# 从用户列表查找索引；未找到返回 -1
def find_user(users, name):
    for i, u in enumerate(users):
        if u.get_username() == name:
            return i
    return -1


# 从管理员列表查找索引；未找到返回 -1
def find_admin(admins, name):
    for i, a in enumerate(admins):
        if a.get_username() == name:
            return i
    return -1


# 从图书列表查找索引；未找到返回 -1
def find_book(books, book_id):
    for i, b in enumerate(books):
        if b.get_id() == book_id:
            return i
    return -1


# 显示当前所有图书
def show_books(books):
    if not books:
        print("（当前没有图书）")
        return
    print("------ 图书列表 ------")
    for b in books:
        b.print()
    print("----------------------")


# 浏览并借阅图书（用户功能）
def borrow_books(books, user):
    if not books:
        print("当前没有任何图书可借。")
        return
    show_books(books)
    book_id = input("请输入要借阅的图书编号（输入 q 取消）：")
    if book_id == "q":
        return

    idx = find_book(books, book_id)
    if idx == -1:
        print("未找到该图书。")
        return
    if user.has_borrowed(book_id):
        print("您已借阅过这本书，不能重复借阅。")
        return
    if books[idx].get_stock() <= 0:
        print("该书当前无库存，无法借阅。")
        return
    books[idx].borrow_one()       # 库存减一
    user.borrow_book(book_id)     # 记录借阅
    print("借阅成功：《{}》".format(books[idx].get_name()))


# 归还图书（用户功能）
def return_books(books, user):
    if user.borrowed_count() == 0:
        print("您当前没有借阅任何图书。")
        return
    print("您当前已借阅：")
    ids = user.get_borrowed_ids()
    for bid in ids:
        bi = find_book(books, bid)
        if bi != -1:
            print("  - {}：《{}》".format(bid, books[bi].get_name()))
        else:
            print("  - {}".format(bid))
    book_id = input("请输入要归还的图书编号（输入 q 取消）：")
    if book_id == "q":
        return

    if user.return_book(book_id):     # 移除借阅记录
        bi = find_book(books, book_id)
        if bi != -1:
            books[bi].return_one()    # 库存加一
            print("归还成功：{}".format(book_id))
        else:
            print("归还成功（该书已不在馆藏列表中）。")
    else:
        print("归还失败：您没有借阅该书。")


# 用户主菜单
def user_menu(books, user):
    while True:
        print("\n===== 用户菜单：《{}》 已登录 =====".format(user.get_username()))
        print("1. 查看图书列表")
        print("2. 借阅图书")
        print("3. 归还图书")
        print("4. 查看我的借阅")
        print("5. 退出登录")
        choice = input("请选择：")

        if choice == "1":
            show_books(books)
        elif choice == "2":
            borrow_books(books, user)
        elif choice == "3":
            return_books(books, user)
        elif choice == "4":
            if user.borrowed_count() == 0:
                print("您当前没有借阅任何图书。")
            else:
                ids = user.get_borrowed_ids()
                print("我借阅的图书：")
                for bid in ids:
                    bi = find_book(books, bid)
                    if bi != -1:
                        print("  - {}：《{}》".format(bid, books[bi].get_name()))
        elif choice == "5":
            print("已退出登录。")
            break
        else:
            print("无效选项，请重新输入。")


# 管理员主菜单
def admin_menu(books, admin):
    while True:
        print("\n===== 管理员菜单：《{}》 已登录 =====".format(admin.get_username()))
        print("1. 查看图书列表")
        print("2. 图书入库")
        print("3. 图书出库")
        print("4. 退出登录")
        choice = input("请选择：")

        if choice == "1":
            show_books(books)
        elif choice == "2" or choice == "3":
            if choice == "2":
                print("（图书入库）")
            else:
                print("（图书出库）")
            book_id = input("请输入图书编号：")
            bname = ""
            if choice == "2":
                bname = input("请输入书名：")
            try:
                count = int(input("请输入数量："))
            except ValueError:
                print("数量必须为整数。")
                continue
            if count < 0:
                print("数量不能为负数。")
                continue
            if choice == "2":
                Admin.add_book(books, book_id, bname, count)   # 入库
            else:
                Admin.remove_book(books, book_id, count)       # 出库
        elif choice == "4":
            print("已退出登录。")
            break
        else:
            print("无效选项，请重新输入。")


def main():
    books = []     # 图书集合
    users = []     # 用户列表
    admins = []    # 管理员列表

    # 预置一个默认管理员账号 admin/admin123
    admins.append(Admin("admin", "admin123"))

    while True:
        print("\n========= 图书管理系统 =========")
        print("1. 注册")
        print("2. 登录")
        print("3. 退出系统")
        choice = input("请选择：")

        if choice == "1":
            # ---- 注册 ----
            role = input("注册为：1. 管理员  2. 用户\n")
            name = input("请输入用户名：")
            pwd = input("请输入密码：")

            if role == "1":
                if find_admin(admins, name) != -1:
                    print("该管理员用户名已存在，注册失败。")
                else:
                    admins.append(Admin(name, pwd))
                    print("管理员注册成功！")
            elif role == "2":
                if find_user(users, name) != -1:
                    print("该用户名已存在，注册失败。")
                else:
                    users.append(User(name, pwd))
                    print("用户注册成功！")
            else:
                print("无效的注册类型。")
        elif choice == "2":
            # ---- 登录 ----
            role = input("登录为：1. 管理员  2. 用户\n")
            name = input("请输入用户名：")
            pwd = input("请输入密码：")

            if role == "1":
                ai = find_admin(admins, name)
                if ai == -1:
                    print("该管理员不存在。")
                elif admins[ai].get_password() != pwd:
                    print("密码错误！")
                else:
                    print("登录成功！欢迎，管理员 {}".format(name))
                    admin_menu(books, admins[ai])
            elif role == "2":
                ui = find_user(users, name)
                if ui == -1:
                    print("该用户不存在。")
                elif users[ui].get_password() != pwd:
                    print("密码错误！")
                else:
                    print("登录成功！欢迎，{}".format(name))
                    user_menu(books, users[ui])
            else:
                print("无效的登录类型。")
        elif choice == "3":
            print("感谢使用图书管理系统，再见！")
            break
        else:
            print("无效选项，请重新输入。")


if __name__ == "__main__":
    main()
