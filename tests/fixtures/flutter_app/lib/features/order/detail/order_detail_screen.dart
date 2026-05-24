import 'package:flutter/material.dart';
import '../../../data/model/order_detail_model.dart';
import 'order_detail_cubit.dart';

class OrderDetailScreen extends StatelessWidget {
  static const routeName = '/orders/:id';

  final OrderDetailCubit cubit;
  final OrderDetailModel order;

  const OrderDetailScreen({super.key, required this.cubit, required this.order});

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Chi tiết đơn hàng')),
      body: Column(
        children: [
          Text(order.statusLabel),
          Visibility(
            visible: order.canCancel == true && order.status != OrderStatus.shipping,
            child: ElevatedButton(
              onPressed: () => cubit.cancelOrder(order.id),
              child: const Text('Hủy đơn'),
            ),
          ),
        ],
      ),
    );
  }
}
