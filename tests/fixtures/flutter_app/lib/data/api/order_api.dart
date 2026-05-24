import '../model/order_detail_model.dart';

class OrderApi {
  final DioClient client;
  OrderApi(this.client);

  Future<OrderDetailModel> getOrderDetail(String id) async {
    final json = await client.get('/orders/$id');
    return OrderDetailModel.fromJson(json);
  }

  Future<void> cancelOrder(String id) async {
    await client.post('/orders/$id/cancel');
  }
}

class DioClient {
  Future<Map<String, dynamic>> get(String path) async => {};
  Future<Map<String, dynamic>> post(String path) async => {};
}
