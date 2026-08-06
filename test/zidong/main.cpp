// 编写日期：2026年8月5日
// 作者：zidong
// 程序说明：二叉树最大路径和求解程序

#include <iostream>
#include <queue>
#include <vector>
#include <string>
#include <limits>
#include <algorithm>

using std::cin;
using std::cout;
using std::string;
using std::vector;
using std::queue;

struct TreeNode {
    int val;
    TreeNode* left;
    TreeNode* right;
    TreeNode(int x) : val(x), left(nullptr), right(nullptr) {}
};

// 释放整棵树，避免内存泄漏
void destroyTree(TreeNode* node) {
    if (!node) return;
    destroyTree(node->left);
    destroyTree(node->right);
    delete node;
}

// 根据层序序列（"null" 表示空节点）构建二叉树
TreeNode* buildTree(const vector<string>& tokens) {
    if (tokens.empty() || tokens[0] == "null") {
        return nullptr;
    }

    TreeNode* root = new TreeNode(std::stoi(tokens[0]));
    queue<TreeNode*> q;
    q.push(root);
    int idx = 1;
    size_t n = tokens.size();

    while (!q.empty() && idx < n) {
        TreeNode* node = q.front();
        q.pop();

        // 左子树
        if (idx < n && tokens[idx] != "null") {
            node->left = new TreeNode(std::stoi(tokens[idx]));
            q.push(node->left);
        }
        idx++;

        // 右子树
        if (idx < n && tokens[idx] != "null") {
            node->right = new TreeNode(std::stoi(tokens[idx]));
            q.push(node->right);
        }
        idx++;
    }
    return root;
}

int best; // 全局最大路径和

// 返回从 node 向下延伸的最大"单边"路径和；
// 同时用穿过各节点的完整路径更新全局 best
int maxPathDown(TreeNode* node) {
    if (!node) return 0;
    int leftSum  = std::max(0, maxPathDown(node->left));
    int rightSum = std::max(0, maxPathDown(node->right));
    // 关键修复：考虑穿过当前节点连通左右子树的完整路径
    best = std::max(best, node->val + leftSum + rightSum);
    // 返回单边路径和（向上层传递）
    return node->val + std::max(leftSum, rightSum);
}

int main() {
    int k;
    cin >> k;

    vector<string> tokens(k);
    for (int i = 0; i < k; i++) {
        cin >> tokens[i];
    }

    TreeNode* root = buildTree(tokens);
    if (!root) {
        cout << 0;
        return 0;
    }

    best = std::numeric_limits<int>::min();
    maxPathDown(root);

    cout << best;

    destroyTree(root);
    return 0;
}
