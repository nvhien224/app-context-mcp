import '../../../data/api/order_api.dart';
import '../../../data/model/order_detail_model.dart';

class OrderDetailCubit {
  final OrderApi api;

  OrderDetailCubit(this.api);

  Future<OrderDetailModel> loadOrder(String id) async {
    return api.getOrderDetail(id);
  }

  Future<void> cancelOrder(String id) async {
    await api.cancelOrder(id);
  }
}
