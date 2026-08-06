// 编写日期：2026年8月5日
// 作者：zidong
// 程序说明：用邻接表构造一个10个节点的有向图

#include <iostream>
#include <vector>

using std::cout;
using std::endl;
using std::vector;

// 邻接表方式的有向图
class DirectedGraph {
public:
    // 构造 n 个节点的空有向图（节点编号 0 ~ n-1）
    DirectedGraph(int n) : adj(n), nodeCount(n) {}

    // 添加一条有向边 from -> to
    void addEdge(int from, int to) {
        adj[from].push_back(to);
    }

    // 打印图的邻接表表示
    void print() const {
        for (int i = 0; i < nodeCount; ++i) {
            cout << "节点 " << i << " -> ";
            if (adj[i].empty()) {
                cout << "(无出边)";
            } else {
                for (size_t j = 0; j < adj[i].size(); ++j) {
                    if (j > 0) cout << " -> ";
                    cout << adj[i][j];
                }
            }
            cout << endl;
        }
    }

private:
    vector<vector<int>> adj; // 邻接表：adj[i] 存放节点 i 的所有出边指向的节点
    int nodeCount;           // 节点个数
};

int main() {
    const int N = 10;                 // 10 个节点
    DirectedGraph graph(N);

    // 添加若干条有向边，构成一个有向图
    graph.addEdge(0, 1);
    graph.addEdge(0, 2);
    graph.addEdge(1, 3);
    graph.addEdge(1, 4);
    graph.addEdge(2, 5);
    graph.addEdge(3, 6);
    graph.addEdge(4, 7);
    graph.addEdge(5, 8);
    graph.addEdge(6, 9);
    graph.addEdge(7, 9);
    graph.addEdge(8, 9);

    // 输出邻接表结构
    cout << "10 个节点的有向图（邻接表表示）：" << endl;
    graph.print();

    return 0;
}
